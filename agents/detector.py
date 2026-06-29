"""Detector specialist — lifecycle management, baseline learning, provisioning."""

from config import AgentConfig
from agent_loop import run_agent
from tools.provisioner import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on detector lifecycle management \
for Splunk Observability Cloud. Your scope is EXCLUSIVELY the environment you are given.

Responsibilities:
1. Discover services and identify which have no detector coverage
2. Learn behavioral baselines from live telemetry (p50/p95/p99, error rates, req rates)
3. Provision or recommend best-practice detectors tuned to actual traffic patterns
4. Retune existing detectors when baselines have drifted
5. Audit deployed detectors for effectiveness

GenAI/agentic services are auto-detected and get specialized detectors.
In dry-run mode, describe exactly what would be deployed with threshold values.
In auto-apply mode, deploy it.
"""

_TASK = """\
Run a complete detector lifecycle assessment:
1. provision_detectors — discover services, learn baselines, recommend/deploy detectors. \
   Use default parameters (do NOT set reconcile=True or skip_baseline=True unless explicitly \
   requested). If the tool returns a timeout error, report what you have — do NOT retry.
2. audit_detectors — check quality of any existing service detectors

After completing both checks, call submit_findings with your structured results.
In issues, include every dark (uncovered) service and every failing detector.
In metrics, include:
  - deployed_count: total detectors deployed or verified
  - dark_service_count: services with no detector coverage
  - deployed_ids: list of detector IDs deployed or confirmed this run (for feedback tracking)
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **TOOL_FNS,
        "submit_findings": make_submit_fn(collector, "detector"),
    }

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        model_id=config.bedrock_model_id,
        region=config.aws_region,
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "detector" in collector:
        result = collector["detector"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(
        domain="detector", summary=raw_text[:500], raw_text=raw_text
    )
