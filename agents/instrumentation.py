"""Instrumentation specialist — span/metric/log attribute quality scoring."""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.analyzer import SCHEMAS, TOOL_FNS
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

After completing your analysis, call submit_findings with your structured results.
In instrumentation_score, provide your 0-100 overall quality score.
In issues, include every gap ranked by severity with the exact fix.
In services_silent, list services with no traces or metrics.
In metrics, include: score, span_coverage_pct, and any other key percentages.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **TOOL_FNS,
        "submit_findings": make_submit_fn(collector, "instrumentation"),
    }

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "instrumentation" in collector:
        result = collector["instrumentation"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(
        domain="instrumentation", summary=raw_text[:500], raw_text=raw_text
    )
