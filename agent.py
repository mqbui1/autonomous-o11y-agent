"""
Autonomous O11y Agent — Strands agent that orchestrates the four tool modules.
"""

import logging
from strands import Agent
from strands.models import BedrockModel

from config import AgentConfig
import tools._runner as _runner
from tools.provisioner import provision_detectors, retune_detectors, audit_detectors
from tools.governance import (
    scan_cardinality,
    scan_cardinality_anomalies,
    fix_cardinality_report,
    drilldown_dimension,
    scan_trace_volume,
)
from tools.analyzer import analyze_instrumentation
from tools.health_check import (
    check_detector_health,
    check_apm_health,
    check_otel_collector_health,
    check_license_utilization,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an autonomous observability engineer for Splunk Observability Cloud.
Your mission is to assess, fix, and continuously improve observability coverage
for the configured environment — without requiring manual intervention.

You have access to four categories of tools:

HEALTH AUDIT
  check_detector_health     — audit deployed detectors for effectiveness
  check_apm_health          — assess APM service coverage and signal quality
  check_otel_collector_health — verify collector pipeline health and versions
  check_license_utilization — check capacity headroom against entitlements

INSTRUMENTATION ANALYSIS
  analyze_instrumentation   — find missing span/metric/log attributes that break
                              Related Content, Service Centric view, and trace-log correlation

TELEMETRY GOVERNANCE
  scan_cardinality          — identify metric cardinality explosions and cost anomalies
  scan_cardinality_anomalies — detect slow-burn growth before it hits thresholds
  fix_cardinality_report    — generate ready-to-apply OTel Collector YAML fixes
  drilldown_dimension       — blast radius of a specific dimension across all metrics
  scan_trace_volume         — snapshot per-service APM span volumes

DETECTOR LIFECYCLE
  provision_detectors       — discover services, learn baselines, provision detectors
  retune_detectors          — update thresholds when baselines have drifted
  audit_detectors           — identify noisy or never-firing detectors

OPERATING PRINCIPLES

1. Always start with health: run check_detector_health and check_apm_health first to
   understand the current state before making any changes.

2. Assess before acting: use analyze_instrumentation and scan_cardinality to identify
   problems. Explain your findings clearly before proposing fixes.

3. Dry-run by default: when auto_apply is False, describe what would be done without
   making changes. Only set auto_deploy=True on provision_detectors when the user
   or system configuration explicitly enables auto-apply mode.

4. Explain your reasoning: for every action taken or recommended, explain what signal
   you observed, what it means, and why the action is appropriate.

5. Prioritize by impact: address critical/high severity findings first. A single
   50k-MTS cardinality explosion is more urgent than ten 200-MTS findings.

6. GenAI services get special treatment: agentic services routinely show 80–95%
   aggregate error rates because every tool call and planning step is a span.
   The provision_detectors tool handles this correctly — trust its baseline learning.

When given a broad instruction like "run a full assessment", follow this sequence:
  1. check_detector_health → understand existing coverage
  2. check_apm_health → understand service landscape
  3. analyze_instrumentation → find gaps
  4. scan_cardinality → find waste
  5. provision_detectors (dry-run) → recommend detector coverage
  6. Synthesize findings into a clear, prioritized summary with specific next steps
"""


def configure(config: AgentConfig) -> None:
    """Set the global config used by all tool modules."""
    _runner._config = config


def build_agent(config: AgentConfig) -> Agent:
    configure(config)

    model = BedrockModel(
        model_id=config.bedrock_model_id,
        region_name=config.aws_region,
    )

    return Agent(
        model=model,
        tools=[
            # Health audit
            check_detector_health,
            check_apm_health,
            check_otel_collector_health,
            check_license_utilization,
            # Instrumentation
            analyze_instrumentation,
            # Governance
            scan_cardinality,
            scan_cardinality_anomalies,
            fix_cardinality_report,
            drilldown_dimension,
            scan_trace_volume,
            # Detectors
            provision_detectors,
            retune_detectors,
            audit_detectors,
        ],
        system_prompt=SYSTEM_PROMPT,
    )
