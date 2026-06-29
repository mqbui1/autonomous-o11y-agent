"""Health specialist — detector quality, APM coverage, collector health, license."""

from config import AgentConfig
from agent_loop import run_agent
from tools.health_check import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on health assessment for Splunk \
Observability Cloud. Your scope is EXCLUSIVELY the environment you are given — ignore \
all findings from other environments or unrelated services.

Responsibilities:
1. Audit detector quality: ghost/noisy/never-fired/muted/inactive-destination detectors
2. Assess APM service coverage: silent services, health check span pollution, sensitive \
data exposure, orphan services
3. Verify OTel Collector health: version status, pipeline errors, stopped collectors
4. Check license utilization headroom

Report findings with specific service names, detector names, counts, and recommendations.
"""

_TASK = """\
Run a complete health assessment:
1. check_detector_health — audit detector quality
2. check_apm_health — find silent services, span pollution, sensitive data
3. check_otel_collector_health — verify pipeline health
4. check_license_utilization — check capacity headroom

After completing all checks, call submit_findings with your structured results.
In the issues list, include every finding ranked by severity.
In services_silent, list every service with no telemetry.
In metrics, include: detectors_healthy, detectors_critical, silent_service_count.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "health")}

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        model_id=config.bedrock_model_id,
        region=config.aws_region,
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "health" in collector:
        result = collector["health"]
        result.raw_text = raw_text
        return result

    # Fallback: agent didn't call submit_findings
    return SpecialistFindings(domain="health", summary=raw_text[:500], raw_text=raw_text)
