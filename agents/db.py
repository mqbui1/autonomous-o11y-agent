"""
Database and dependency specialist — proactive analysis of DB health and
external dependency blind spots.

Unlike the RCA specialist (which investigates a specific incident), this specialist
runs proactively on every assessment cycle to surface issues before they become
incidents:

1. Service dependency topology — which services depend on which databases/external APIs,
   including inferred (unmonitored) service nodes that are complete blind spots
2. Database instrumentation quality — which services are missing db.system, db.name,
   or db.operation attributes that are required for APM Database Overview
3. Slow outbound calls — proactively surfaces high-latency DB queries and external
   API calls before they cause user-visible incidents
4. Dependency error rates — identifies services with high outbound error rates,
   indicating an upstream dependency is degrading

The key insight: most latency issues originate in dependencies (slow DB query, rate-
limited external API, saturated connection pool), not in the calling service's own code.
This specialist makes those dependencies visible.
"""

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from tools.db_tools import SCHEMAS, TOOL_FNS
from tools.findings import SUBMIT_SCHEMA, SpecialistFindings, make_submit_fn

_SYSTEM = """\
You are a specialist observability engineer focused on database and external dependency \
health for Splunk Observability Cloud. Your scope is the environment you are given.

Responsibilities:
1. Map the full service dependency topology including inferred (unmonitored) service nodes
2. Identify inferred DB and external API nodes — these are blind spots with no observability
3. Assess database span instrumentation quality: db.system, db.name, db.operation coverage
4. Surface slow outbound calls proactively: high-latency DB queries and external API calls
5. Find services with high outbound error rates (dependency errors cascading inward)

Key signals and what they mean:
- Inferred DB node in topology → service makes DB calls but db.* attributes are missing or
  the DB is not instrumented, so you cannot see query-level performance
- db.system present but db.statement absent → cannot see the actual slow query text
- High error rate on outbound calls → the dependency (DB, external API) is the likely culprit,
  not the calling service
- Slow trace duration concentrated in one service's outbound span → that call is the bottleneck

Severity guidance:
- Inferred service node handling >10% of a critical service's traffic → high
- Service with DB calls but no db.* attributes → high (instrumentation gap for core feature)
- Service with outbound error rate >5% → critical
- Service with outbound error rate >1% → high
- Slow outbound call (p99 >500ms to a DB or external API) → medium/high depending on SLA
"""

_TASK = """\
Run a complete database and dependency assessment:

1. get_service_dependency_map — map all service dependencies including inferred nodes.
   Note which inferred nodes appear to be databases vs external APIs.
2. find_db_instrumented_services — check db.* attribute coverage for all services
   making database calls. Identify services with missing db.system, db.name, or db.operation.
3. get_outbound_call_error_rates — find services with high outbound error rates.
   High rates here indicate a dependency is unhealthy.
4. search_slow_outbound_calls — find the slowest traces in the last hour.
   These reveal which outbound calls are latency bottlenecks.

After completing all checks, call submit_findings with:
- summary: 2-4 sentences: how many inferred (unmonitored) dependencies exist, which services
  have the worst db instrumentation, any high outbound error rates.
- issues: one per critical finding (inferred blind-spot dependencies, missing db attrs, high error rates)
- services_active: instrumented services discovered in the topology
- metrics: {
    "inferred_dependency_count": <count of unmonitored inferred services>,
    "db_blind_spot_count": <services making DB calls with no db.* attributes>,
    "max_outbound_error_rate_pct": <highest outbound error rate seen>,
    "services_with_db_instrumentation": <count fully instrumented>
  }
"""


def run(config: AgentConfig, state_context: str = "") -> SpecialistFindings:
    collector: dict = {}
    all_schemas = SCHEMAS + [SUBMIT_SCHEMA]
    all_tool_fns = {**TOOL_FNS, "submit_findings": make_submit_fn(collector, "db")}

    prompt = f"{state_context}\n\n---\n\n{_TASK}" if state_context else _TASK
    raw_text = run_agent(
        provider=get_provider(config),
        system_prompt=_SYSTEM + f'\n\nEnvironment: "{config.environment}"',
        tools=all_schemas,
        tool_fns=all_tool_fns,
        initial_message=prompt,
    )

    if "db" in collector:
        result = collector["db"]
        result.raw_text = raw_text
        return result

    return SpecialistFindings(domain="db", summary=raw_text[:500], raw_text=raw_text)
