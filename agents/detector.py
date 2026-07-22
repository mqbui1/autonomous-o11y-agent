"""Detector specialist — lifecycle management, baseline learning, provisioning."""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.provisioner import SCHEMAS, TOOL_FNS
from tools.adoption_tools import SCHEMAS as ADOPTION_SCHEMAS, TOOL_FNS as ADOPTION_FNS
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
3. get_broken_detectors — find detectors with no notification rules or that are disabled.
   Flag these as high-severity issues: a detector that fires to nobody is a broken smoke alarm.
In metrics, include:
  - deployed_count: total detectors deployed or verified
  - dark_service_count: services with no detector coverage
  - deployed_ids: list of detector IDs deployed or confirmed this run (for feedback tracking)
In actions_taken, include one entry per detector actually deployed or retuned this run.
Example entry: "Deployed detector HxYZ for payment-service (latency p95 > 500ms threshold)"
Leave actions_taken empty if this is a dry-run with no changes applied.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [s for s in ADOPTION_SCHEMAS if s.get("toolSpec", {}).get("name") == "get_broken_detectors"] + [SUBMIT_SCHEMA]
    all_tool_fns = {
        **TOOL_FNS,
        "get_broken_detectors": ADOPTION_FNS["get_broken_detectors"],
        "submit_findings": make_submit_fn(collector, "detector"),
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

    if "detector" in collector:
        result = collector["detector"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(
        domain="detector", summary=raw_text[:500], raw_text=raw_text
    )
