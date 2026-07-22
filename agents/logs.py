"""Log analysis specialist — error patterns, log volume, coverage gaps."""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.log_analyzer import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on log analysis for \
Splunk Observability Cloud. Your scope is EXCLUSIVELY the environment you are given.

Responsibilities:
1. Identify services generating high error log volumes and surface the top recurring patterns
2. Find services with zero log output (complete logging gap)
3. Detect log volume anomalies — services logging orders of magnitude more than peers
4. Identify log quality issues: missing trace/span IDs for correlation, missing severity fields
5. Distinguish actionable errors (new, high-count patterns) from log noise (expected retries, etc.)

Always report specific service names, error counts, and example log messages.
"""

_TASK = """\
Run a complete log analysis assessment:
1. get_log_volume — identify which services have logs, which have zero output, and any volume anomalies
2. search_error_logs — find services with significant ERROR/CRITICAL entries
3. analyze_log_patterns — identify the top recurring error patterns and noise sources

After completing all checks, call submit_findings with your structured results.
In services_silent, include services with zero log output.
In issues, include every significant finding ranked by severity.
In metrics, include: total_error_count, services_with_logs, services_without_logs,
  top_error_service (service name with most errors), log_coverage_pct (0-100).
In actions_taken, list any automated log sampling rules or suppressions applied (if auto_apply).
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "logs")}

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
        max_turns=getattr(config, "specialist_max_turns", 8),
    )

    if "logs" in collector:
        result = collector["logs"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="logs", summary=raw_text[:500], raw_text=raw_text)
