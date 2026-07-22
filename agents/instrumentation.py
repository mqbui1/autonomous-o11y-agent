"""Instrumentation specialist — span/metric/log attribute quality scoring."""

import json

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.analyzer import SCHEMAS, TOOL_FNS
from tools.adoption_tools import SCHEMAS as ADOPTION_SCHEMAS, TOOL_FNS as ADOPTION_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on instrumentation quality for \
Splunk Observability Cloud. Your scope is EXCLUSIVELY the environment you are given.

Responsibilities:
1. Analyze APM span attributes for missing critical fields (deployment.environment, \
host.name, k8s.* attrs, http.method, http.status_code)
2. Assess infrastructure metric dimensions for correlation gaps
3. Check log signal presence and trace/span injection fields
4. Score each signal type 0–100 and explain the UX impact at current coverage levels
5. Connect every gap to its concrete UX impact: which Related Content links break, \
which Service Centric View tabs are empty, which K8s Navigator tiles are missing

Always report specific attribute names, coverage percentages, and exact fixes.
"""

_TASK = """\
Run a complete instrumentation quality assessment:
1. analyze_instrumentation — score APM, metrics, and logs; find all attribute gaps
2. get_sdk_coverage — check which OTel SDK languages/versions are in use org-wide.
   Flag any pre-1.0 (pre-stable) SDK versions as high severity — they use unstable
   semantic conventions that cause attribute name mismatches and APM gaps.

After completing your analysis, call submit_findings with your structured results.
NOTE: Do NOT set instrumentation_score — it is computed automatically from the
analyzer output. Focus on quality narrative in summary.
In issues, include every gap ranked by severity with the exact fix.
In services_silent, list services with no traces or metrics.
In metrics, include: apm_score, metrics_score, logs_score, span_coverage_pct,
and any other key percentages extracted directly from the analyzer JSON output.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    # Capture the raw analyzer JSON so we can extract deterministic scores
    # without relying on the LLM to pick a number.
    _raw_analyzer: dict = {}

    _orig_analyze = TOOL_FNS["analyze_instrumentation"]

    def _capturing_analyze(**kwargs):
        result_str = _orig_analyze(**kwargs)
        try:
            data = json.loads(result_str)
            # Keep the latest call (all-services scan is typically the first/only call)
            if "correlation" in data:
                _raw_analyzer.update(data)
        except Exception:
            pass
        return result_str

    all_schemas = SCHEMAS + [s for s in ADOPTION_SCHEMAS if s.get("toolSpec", {}).get("name") == "get_sdk_coverage"] + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **TOOL_FNS,
        "analyze_instrumentation": _capturing_analyze,
        "get_sdk_coverage": ADOPTION_FNS["get_sdk_coverage"],
        "submit_findings": make_submit_fn(collector, "instrumentation"),
    }

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
        max_turns=getattr(config, "specialist_max_turns", 8),
    )

    if "instrumentation" in collector:
        result = collector["instrumentation"]
        result.raw_text = raw_text
        # Override the LLM-chosen score with the analyzer's own computed value.
        # combined_score is the average of apm/metrics/logs scores — fully deterministic.
        if _raw_analyzer:
            corr = _raw_analyzer.get("correlation", {})
            computed = corr.get("combined_score")
            if computed is not None:
                result.instrumentation_score = int(computed)
            # Inject sub-scores into metrics so the UI and trend context can show them
            result.metrics.setdefault("apm_score", _raw_analyzer.get("apm", {}).get("score", 0))
            result.metrics.setdefault("metrics_score", _raw_analyzer.get("metrics", {}).get("score", 0))
            result.metrics.setdefault("logs_score", _raw_analyzer.get("logs", {}).get("score", 0))
            result.metrics["score"] = result.instrumentation_score  # keep "score" alias in sync
        return result

    return SpecialistFindings(
        domain="instrumentation", summary=raw_text[:500], raw_text=raw_text
    )
