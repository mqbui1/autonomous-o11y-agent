"""RUM (Real User Monitoring) specialist — frontend user experience analysis."""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.rum_analyzer import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist frontend observability engineer focused on Real User Monitoring \
(RUM) for Splunk Observability Cloud. Your scope is EXCLUSIVELY the environment you are given.

Responsibilities:
1. Discover which RUM applications are instrumented and reporting data
2. Assess session volume, JavaScript error rates, and Core Web Vitals (LCP, FID, CLS)
3. Identify unconfigured frontends — services with no RUM data despite expected user traffic
4. Surface high error rates, poor Core Web Vitals, and the top recurring JS error types
5. Distinguish instrumentation gaps (RUM not configured) from actual UX degradation

Core Web Vitals thresholds (Google/Splunk RUM):
  LCP: good <2500ms, needs-improvement <4000ms, poor >=4000ms
  FID: good <100ms, needs-improvement <300ms, poor >=300ms
  CLS: good <0.1, needs-improvement <0.25, poor >=0.25
"""

_TASK = """\
Run a complete RUM assessment:
1. list_rum_apps — discover which frontend apps are instrumented
2. For each configured app: get_rum_metrics — session counts, error rate, Core Web Vitals
3. For any app with error_rate >2% or poor Core Web Vitals: get_rum_errors — top JS error types

After completing all checks, call submit_findings with your structured results.
In services_silent, include frontend services with zero RUM data (not configured).
In issues, include every significant finding ranked by severity.
In metrics, include: total_sessions, total_js_errors, apps_configured, apps_unconfigured,
  worst_lcp_ms (highest p75 LCP across all apps), avg_error_rate_pct.
In actions_taken, leave empty for dry-run.
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "rum")}

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM,
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "rum" in collector:
        result = collector["rum"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="rum", summary=raw_text[:500], raw_text=raw_text)
