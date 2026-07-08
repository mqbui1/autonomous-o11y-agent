"""
Performance specialist — CPU profiling, call-pattern analysis, and code-level fix generation.

This specialist bridges the gap between telemetry symptoms and root causes in code:

1. Check AlwaysOn Profiling availability (get_profiling_services)
2. Analyze span call patterns for anti-patterns regardless of profiling state
   (analyze_span_call_patterns — always available)
3. If profiling is enabled: get CPU flame graph, find hot functions by file:line
4. If source code is configured: read the exact function, generate a concrete diff
5. Submit findings with action_args containing file, line, pattern, and suggested_diff

Output quality tiers (best to worst, depending on what's available):
  A. Profiling + Source  → exact file:line + function body + suggested diff
  B. Profiling only      → exact file:line:function + pattern-based fix description
  C. Span patterns only  → operation name + N+1/latency pattern + generic fix
  D. Nothing available   → skip, return empty findings

All tiers produce actionable issues. Tier C is available in EVERY environment.
"""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.profiling_tools import SCHEMAS as PROF_SCHEMAS, TOOL_FNS as PROF_FNS
from tools.source_tools import SCHEMAS as SRC_SCHEMAS, TOOL_FNS as SRC_FNS, _source_mode
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist performance engineer for Splunk Observability Cloud. \
Your job is to find specific, actionable performance problems in code — \
N+1 queries, blocking I/O, hot functions, memory leaks, lock contention — \
and generate concrete fix recommendations with exact file and line references \
when source code is available.

You work in three tiers depending on available data:

TIER A (best): AlwaysOn Profiling + Source Code
- Use get_cpu_flamegraph to find hot stack frames (file:line:function:samples)
- Use get_source_context to read the exact function body
- Generate a concrete diff or annotated fix showing exactly what to change
- Reference the specific line: "at src/cart/repository.py:247 in get_cart_items()"

TIER B+: Call Graph API (trace-correlated profiling, no source)
- Use get_slowest_methods(service, trace_id, from_ms, to_ms) with a trace ID from a slow span
- Returns class + method ranked by exclusive self-time for THAT specific request
- More precise than aggregate flamegraph: pinpoints the exact hot code path
- Describe the fix: "className.methodName consumed Xms self-time — likely due to..."
- Include method + class in action_args

TIER B: AlwaysOn Profiling only (no source)
- Use get_cpu_flamegraph for file:line:function
- Describe the fix pattern precisely: "replace per-item query at line 247 with
  SELECT ... WHERE id IN (...)" — specific enough for a developer to apply immediately
- Include the profiling frame data in action_args

TIER C (always available): Span pattern analysis only
- Use analyze_span_call_patterns for EVERY service regardless of profiling
- Detect N+1 (calls_per_request > 5 on DB operations), latency outliers, hot operations
- Provide operation-level fix recommendations

SCOPE — you own latency, CPU, memory, and query inefficiency only:
- N+1 queries, bulk-load anti-patterns, hot functions, memory leaks, lock contention
- NOT error rates, NOT service availability, NOT dependency failures
  → Those belong to the health and RCA specialists. If a service has a 100% error rate
    with no latency/profiling data to analyze, skip it entirely.

SEVERITY GUIDANCE:
- N+1 query with >20 calls/request → critical
- N+1 query with 5-20 calls/request → high
- CPU hot function consuming >15% CPU → high  (profiling alone is sufficient — no span signal required)
- CPU hot function consuming 10-15% CPU → high if application code, medium otherwise
- Latency outlier (P99 > 10× P50 and P99 > 500ms) → high
- Memory leak (allocation growth) → high
- Lock contention (>10% threads BLOCKED) → high
- Medium issues: report but don't create issues (keep signal-to-noise high)

ISSUE FORMAT — be specific, not generic:
- service: exact service name
- description: "N+1 query in get_cart_items() at src/cart/repository.py:247 —
  43 SELECT calls per request, each fetching one product by ID"
- recommendation: numbered steps with exact fix:
  (1) Replace: `product = db.query(Product).filter_by(id=item.product_id).first()`
  (2) With: `products = db.query(Product).filter(Product.id.in_(product_ids)).all()`
  (3) Redeploy and verify calls_per_request drops to 1 in APM
- action_tool: "generate_code_fix"
- action_args: {
    "file": "src/cart/repository.py",
    "line": 247,
    "function": "get_cart_items",
    "pattern": "n_plus_1_query",
    "db_system": "postgresql",
    "suggested_diff": "--- a/src/cart/repository.py\\n+++ b/src/cart/repository.py\\n...",
    "fix_description": "Replace per-item query with bulk SELECT WHERE id IN (...)"
  }
"""

_TASK = """\
Run a complete performance analysis for this environment:

STEP 1 — Check source code availability:
  Call get_source_status() to know which tier analysis is possible.

STEP 2 — Check profiling availability:
  Call get_profiling_services(environment) to see which services have AlwaysOn Profiling.

STEP 3 — Span pattern analysis (ALWAYS run this for all active services):
  Call analyze_span_call_patterns(service, environment) for each service.
  Focus exclusively on:
    - calls_per_request > 5 on DB/cache operations (N+1 pattern)
    - P99 > 10× P50 on any operation (latency outlier)
  Do NOT flag high error rates — skip services where the primary signal is errors
  rather than latency or call-count anti-patterns. Error rates are the health
  specialist's domain.

STEP 4 — Trace-correlated profiling via Call Graph API (preferred when trace IDs available):
  For each slow trace found in Step 3 (latency outliers, high-error operations),
  call get_slowest_methods(service, trace_id, from_epoch_ms, to_epoch_ms).

  Use the span's timestamp ± 30 seconds as the time window:
    from_epoch_ms = span_timestamp_ms - 30_000
    to_epoch_ms   = span_timestamp_ms + 30_000

  If get_slowest_methods returns profiling_available=True:
  - Use class+method as the primary finding anchor (Tier B+)
  - self_time_ms is exclusive CPU time — the method IS the bottleneck
  - exit_call/exit_call_action reveals what it was blocked on (I/O, locks, sleep)
  - Combine with span anti-pattern from Step 3 for a Tier B+/A finding
  - Proceed to Step 5 to fetch source for these methods (Tier A)

  If get_slowest_methods returns profiling_available=False:
  - Note the error/reason in your reasoning (useful API feedback)
  - Fall through to Step 4b

STEP 4b — Aggregate CPU profiling (fallback when no trace IDs or call graph unavailable):
  Call get_cpu_flamegraph(service, environment) for EVERY service that has
  profiling enabled (from Step 2), regardless of whether Step 3 found span-level
  anti-patterns. Profiling can surface hot functions that span metrics alone
  cannot detect — high CPU consumption, expensive serialization, GC pressure, etc.

  For each flamegraph result:
  - Report any application-code frame (not framework/stdlib) consuming >10% CPU
    as a standalone finding even if no N+1 or latency outlier was found
  - Cross-reference with Step 3 results: if a span anti-pattern AND a profiling
    hot frame point to the same service, combine them into one richer issue
  - Also call get_thread_profile() for services with latency outliers from Step 3.

STEP 5 — Source code (if configured):
  For each hot function found in Step 4, call:
    get_source_context(file_path, line, service) → read the function body
  Then get_source_context for surrounding functions if needed to understand the call chain.
  If you only have a function name (no file from profiling), use search_source_for_function().

STEP 6 — Generate fixes:
  For each confirmed performance issue, generate:
  - If source available: exact before/after code diff in unified diff format
  - If profiling only: precise description of which line/function to change and how
  - If span patterns only: operation-level recommendation with ORM/query fix pattern

STEP 7 — submit_findings with:
  - summary: 2-3 sentences covering worst anti-patterns found, services affected,
    whether profiling/source were available, and estimated impact if fixed
  - issues: one per confirmed finding (only critical/high — skip medium/low)
  - metrics: {
      "profiling_enabled_services": <count with AlwaysOn Profiling>,
      "n_plus_1_services": <count with N+1 patterns>,
      "hotspot_functions_identified": <count with exact file:line from profiling>,
      "code_fixes_generated": <count with concrete diff in action_args>
    }

DO NOT create issues for:
- Framework/stdlib functions in profiling frames (only report application code,
  e.g. skip grpc._channel, opentelemetry.*, flask.*, werkzeug.*, urllib3.*)
- Issues where calls_per_request < 5 (noise)
- Services with < 100 total requests (insufficient sample size)
- Error rates, service failures, or dependency outages — those are health/RCA domain
- Missing detectors or alerting gaps — that is the detector specialist's domain
- Findings where you have no latency or profiling data, only error counts
- Silent services or stub telemetry (service_requests_total=1, zero spans) — that is
  the instrumentation specialist's domain. If a service has no spans to analyze, skip it.
- OTel Collector config, service.name mismatches, SDK initialization — those are
  instrumentation/health domain. Never recommend kubectl commands or env var audits.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}

    # Dynamically include source tools only when source access is configured.
    # This enforces the tier fallback at the tool level — the LLM cannot call
    # source tools if they are not registered, making Tier B automatic.
    source_mode = _source_mode()   # "local" | "github" | "none"
    source_active = source_mode != "none"

    all_schemas = PROF_SCHEMAS + (SRC_SCHEMAS if source_active else []) + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **PROF_FNS,
        **(SRC_FNS if source_active else {}),
        "submit_findings": make_submit_fn(collector, "performance"),
    }

    if source_active:
        tier_note = (
            f"SOURCE CODE: CONFIGURED ({source_mode} mode) — "
            "Tier A analysis available. Use get_source_context() and "
            "search_source_for_function() to read code and generate diffs."
        )
    else:
        tier_note = (
            "SOURCE CODE: NOT CONFIGURED — operate in TIER B (profiling data only). "
            "Do NOT attempt to read source files (no source tools are available). "
            "Generate precise fix descriptions based on file:line:function from "
            "profiling frames. Omit 'suggested_diff' from action_args; include "
            "'fix_description' only."
        )

    task_with_tier = f"{tier_note}\n\n{_TASK}"
    prompt = f"{state_context}\n\n---\n\n{task_with_tier}" if state_context else task_with_tier

    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM + f'\n\nEnvironment: "{config.environment}"',
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
        max_turns=getattr(config, "specialist_max_turns", 10),
    )

    if "performance" in collector:
        result = collector["performance"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="performance", summary=raw_text[:500], raw_text=raw_text)
