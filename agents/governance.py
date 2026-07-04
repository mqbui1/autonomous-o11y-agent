"""Governance specialist — cardinality, cost, and trace volume."""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.governance import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on telemetry governance for \
Splunk Observability Cloud. Your scope is EXCLUSIVELY the environment you are given.

Responsibilities:
1. Identify metric cardinality explosions and cost anomalies
2. Detect slow-burn cardinality growth before it crosses thresholds
3. Generate ready-to-apply OTel Collector YAML fixes for any issues found
4. Snapshot per-service APM trace volumes for anomaly detection

Always quantify findings: MTS count, cost impact, growth rate.
If issues are found, call fix_cardinality_report for remediation YAML.
If a specific dimension is suspect, call drilldown_dimension for blast radius.
"""

_TASK = """\
Run a complete telemetry governance assessment:
1. full_cardinality_scan — runs cardinality scan AND anomaly scan in parallel (use this \
   instead of calling scan_cardinality and scan_cardinality_anomalies separately)
2. scan_trace_volume — snapshot per-service APM span volumes
3. ONLY if step 1 found anomaly_count > 0 AND the governance DB is writable (not read-only), \
   call fix_cardinality_report for remediation YAML. Skip fix_cardinality_report entirely \
   if there are no anomalies or if the DB is read-only — it will not produce useful output.
4. ONLY if step 1 identified a specific dimension as a top cardinality offender, call \
   drilldown_dimension for that dimension. Do NOT call drilldown_dimension speculatively.

After completing all checks, call submit_findings with your structured results.
In issues, include every cardinality explosion and anomaly ranked by severity.
In metrics, include: top_cardinality_mts (highest single metric MTS count), \
anomaly_count, and top_metrics (list of metric names with highest cardinality).
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **TOOL_FNS,
        "submit_findings": make_submit_fn(collector, "governance"),
    }

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "governance" in collector:
        result = collector["governance"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(
        domain="governance", summary=raw_text[:500], raw_text=raw_text
    )
