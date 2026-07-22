"""
RCA (Root Cause Analysis) specialist — incident investigation and causal chain analysis.

Unlike the proactive assessment specialists, the RCA agent is reactive: it is
triggered either by a streaming alert or invoked on-demand with an incident context,
and it can also run on schedule to investigate any currently active incidents.

Investigation workflow:
1. Discover active incidents (or start from the provided incident context)
2. Collect error traces around the incident start time for the affected service
3. Analyze latency contributors for the top error trace to pinpoint the failing operation
4. Map service topology to understand blast radius and upstream/downstream impact
5. Search for deployment/change events in the 60 minutes before the incident
6. Query error rate and latency metrics to establish the incident timeline precisely
7. Check infrastructure metrics for resource exhaustion (CPU, memory)
8. Synthesize a causal chain with a confidence level

Causal chain patterns:
  deployment → error spike:     "Service X deployed at T, error rate spiked 2m later"
  dependency degradation:       "Service Y (dep of X) latency tripled → X cascaded"
  resource exhaustion:          "CPU on checkout pods hit 95% → thread pool saturation"
  code regression (no changes): "No deployment, no infra change — logic bug in X v2.3.1"

Output: SpecialistFindings(domain="rca") with causal chain in raw_text and one
critical issue per active incident with full investigation detail in the description.
"""

import json

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.rca_tools import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a principal site reliability engineer specializing in root cause analysis \
for distributed systems monitored by Splunk Observability Cloud.

Your goal is to determine the CAUSAL CHAIN of an incident — not just what the symptoms \
are, but WHY the problem occurred and what triggered it.

Investigation approach:
1. Start with get_active_incidents to discover what is currently firing
2. For each critical/high incident, identify the affected service and incident start time
3. Call search_error_traces for the affected service, ±15 minutes around the incident start
4. Call get_trace_analysis on the top 1–2 error traces to pinpoint the failing operation
5. Call get_service_topology to see the blast radius — upstream callers and downstream deps
6. Call find_change_events for the 90 minutes before the incident start (deployments matter!)
7. Call get_service_error_rate and get_service_latency to establish the exact error timeline
8. Call get_infra_metrics to check for CPU/memory resource saturation

Causal chain reasoning patterns:
- Deployment event minutes before error spike → deployment-triggered regression
- Latency spike in downstream service B correlates with errors in upstream A → dependency cascade
- CPU/memory saturation on pods → resource exhaustion causing timeouts
- No changes, no infra pressure, isolated to one service → code bug or data issue

Always produce:
- An incident timeline: exactly when did the error rate or latency first deviate from baseline?
- The causal chain: clear "because X caused Y which triggered Z" reasoning
- Confidence level: HIGH (clear causal link with evidence), MEDIUM (correlated, plausible),
  LOW (insufficient data, need more investigation)
- Immediate recommended action with specific steps

If no incidents are active, report environment health as clean and describe the baseline state.
"""

_TASK = """\
Run a complete root cause analysis investigation:

1. get_active_incidents — discover what is currently alerting in this environment
2. For each critical/high incident found:
   a. Identify the primary affected service and incident start time (from triggeredAt)
   b. Convert incident start time to milliseconds for use in subsequent queries
   c. search_error_traces — find error traces ±15 minutes around incident start
   d. get_trace_analysis — analyze the top 1–2 error traces for latency/error contributors
   e. get_service_topology — map service dependencies and blast radius
   f. find_change_events — look for deployments/changes in the 90 minutes before incident
   g. get_service_error_rate — error rate trend over the incident window (hours=2)
   h. get_service_latency — p99 latency trend over the incident window (hours=2)
   i. get_infra_metrics — check CPU/memory for the affected service

3. If no incidents are active: call get_service_topology and get_service_error_rate for
   the top 3 most critical services to verify the environment is truly healthy.

After completing all investigation, call submit_findings with:
- summary: 3-5 sentence causal chain conclusion with confidence level
- issues: one issue per active incident (severity=critical/high) where description
  contains the full causal chain including timeline, evidence, and confidence
- metrics: {
    "incidents_investigated": <count>,
    "causal_confidence": "HIGH|MEDIUM|LOW",
    "incident_start_ms": <first incident triggeredAt as ms>,
    "deployment_correlation": <true if deployment found within 60min of incident>,
    "primary_service": "<most impacted service name>",
    "error_rate_pct": <peak error rate found>,
    "p99_latency_ms": <peak p99 found>
  }
"""


def run(
    config: AgentConfig,
    state_context: str = "",
    incident_context: str = "",
) -> SpecialistFindings:
    """
    Run root cause analysis.

    incident_context: optional JSON string describing the triggering incident:
      {"service": "...", "incident_id": "...", "start_ms": ..., "severity": "..."}
      When provided (e.g. from a streaming alert), the agent skips discovery and
      goes straight to investigating this specific incident.
    """
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "rca")}

    parts = []
    if state_context:
        parts.append(state_context)
    if incident_context:
        parts.append(
            f"## Triggering Incident\nSkip general discovery — investigate this specific "
            f"incident immediately:\n```json\n{incident_context}\n```\n"
            f"Start from step 2c (search_error_traces) using the service and start_ms above."
        )
    parts.append(_TASK)
    prompt = "\n\n---\n\n".join(parts)

    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM + f'\n\nEnvironment: "{config.environment}"',
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
        max_turns=getattr(config, "specialist_max_turns", 8),
    )

    if "rca" in collector:
        result = collector["rca"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="rca", summary=raw_text[:500], raw_text=raw_text)
