"""
Synthetics specialist — external health validation coverage and test quality.

Assesses the Splunk Synthetics footprint:
1. What synthetic tests exist (browser, API, uptime) and which are currently failing
2. Which services have no synthetic coverage — external health blind spots
3. Tests running too infrequently to catch real outages quickly
4. Performance degradation trends: tests getting slower before they fail
5. Regional failures: tests passing in some locations but failing in others

Unlike the health specialist (which checks internal telemetry), this specialist
validates that critical user journeys and service endpoints are being probed
from the outside — the way a real user or downstream consumer experiences them.
"""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.synthetics_tools import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on Splunk Synthetics — external \
health validation for Splunk Observability Cloud. Your scope is the environment you are given.

Responsibilities:
1. Inventory all synthetic tests: browser, API, and uptime checks
2. Identify services and critical user journeys with NO synthetic coverage
3. Surface currently failing or degrading tests with specific error messages
4. Find tests that are misconfigured: inactive, too-infrequent, or single-location only
5. Detect tests showing a performance degradation trend (getting slower over time)
6. Identify regional failures: tests passing in one location but failing in another

Coverage philosophy:
- Every user-facing service should have at least one uptime test (5-minute frequency)
- Critical transactions (checkout, login, payment) need browser tests
- APIs consumed by external parties need API tests
- A service that has internal APM coverage but no synthetics is still a blind spot —
  APM tells you what happened after a user hit the service; synthetics tells you
  if the service is reachable at all

Severity guidance:
- Test currently failing → critical
- Test showing degrading trend (>20% slower) → high
- Service with no synthetic coverage → high (if user-facing) or medium (internal)
- Test inactive or paused → medium
- Single-location test (no geo diversity) → low
"""

_TASK = """\
Run a complete synthetics assessment:

1. list_synthetics_tests — get all tests, note which are failing, inactive, or low-frequency
2. For each FAILING test: get_test_results — get error details and failure rate
3. For tests running >48h: get_test_performance_trend — check if duration is degrading
4. get_synthetics_coverage_gaps — pass in the list of services you know about from the
   APM environment (use any service names visible from failing tests or your general knowledge
   of the environment). Identify which services have no external health validation.

After completing all checks, call submit_findings with:
- summary: 2-4 sentences covering: how many tests exist, how many are failing, biggest coverage gaps
- issues: one issue per failing test (critical) and one per major coverage gap (high)
- services_silent: services with no synthetic coverage at all
- metrics: {
    "total_tests": <count>,
    "failing_tests": <count>,
    "inactive_tests": <count>,
    "coverage_gap_count": <services with no synthetics>,
    "avg_uptime_pct": <average uptime across all tests with data>
  }
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "synthetics")}

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM + f'\n\nEnvironment: "{config.environment}"',
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "synthetics" in collector:
        result = collector["synthetics"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="synthetics", summary=raw_text[:500], raw_text=raw_text)
