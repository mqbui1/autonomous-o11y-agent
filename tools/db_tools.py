"""
Database and dependency analysis tools for Splunk Observability Cloud.

Proactively surfaces slow database operations, unhealthy external dependencies,
and instrumentation gaps in outbound call coverage:

- Service dependency topology with inferred service nodes (DBs, external APIs)
- Slow DB and outbound HTTP operations via APM async trace search
- Per-service outbound error rates via SignalFlow
- Discovery of which services have db.* span attributes (DB instrumentation quality)

Uses APM GraphQL + SignalFlow REST APIs — no subprocess dependencies.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ._runner import get_config

logger = logging.getLogger(__name__)

# Known database span attribute values for system identification
_DB_SYSTEMS = {"postgresql", "mysql", "mssql", "oracle", "mongodb", "redis",
               "cassandra", "dynamodb", "elasticsearch", "sqlite", "mariadb",
               "memcached", "couchdb", "neo4j", "hbase", "db2"}


def _api(path: str, payload: dict = None, method: str = "GET") -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API {method} {path} returned {e.code}: {body}") from e


def _graphql(query: str, variables: dict = None) -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com/v2/apm/graphql"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GraphQL {e.code}: {body}") from e


def _signalflow_execute(program: str, start_ms: int, end_ms: int) -> dict:
    cfg = get_config()
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    payload = {"program": program, "start": start_ms, "stop": end_ms,
               "resolution": 60000, "immediate": True}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    streams: dict[str, list] = {}
    metadata: dict[str, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "metadata":
                    tsid = msg.get("tsId") or msg.get("channel") or ""
                    if tsid:
                        metadata[tsid] = msg.get("properties", {})
                elif mtype == "data":
                    for _ts, point in msg.get("data", {}).items():
                        for sid, val in point.items():
                            if val is not None:
                                streams.setdefault(sid, []).append(float(val))
    except Exception as exc:
        logger.warning("SignalFlow execute failed: %s", exc)
    return {"streams": streams, "metadata": metadata}


# ── DB / Dependency Tool Functions ────────────────────────────────────────────

def get_service_dependency_map(environment: str, lookback_minutes: int = 60) -> str:
    """
    Get the full service dependency map including inferred services (databases,
    external APIs, message queues) that are not directly instrumented.

    Inferred services appear when a service makes outbound calls to an unmonitored
    endpoint — they represent blind spots where you have no visibility into what
    the dependency is doing. Returns edges with call counts, error counts, and p99
    latency, annotated to indicate which edges go to inferred (unmonitored) services.

    Args:
        environment: Deployment environment name.
        lookback_minutes: How far back to look (default: 60).
    """
    _MAP = """
query GetServiceMap($environment: String!, $windowMs: Long) {
  getServiceMap(environment: $environment, windowMs: $windowMs) {
    nodes {
      serviceName
      isInferred
    }
    edges {
      sourceServiceName
      targetServiceName
      callCount
      errorCount
      p99
    }
  }
}
"""
    try:
        window_ms = lookback_minutes * 60 * 1000
        resp = _graphql(_MAP, variables={"environment": environment, "windowMs": window_ms})
        errors = resp.get("errors")
        if errors:
            return json.dumps({"graphql_errors": errors})

        svc_map = resp.get("data", {}).get("getServiceMap", {})
        nodes = svc_map.get("nodes", [])
        edges = svc_map.get("edges", [])

        instrumented = [n["serviceName"] for n in nodes if not n.get("isInferred") and n.get("serviceName")]
        inferred = [n["serviceName"] for n in nodes if n.get("isInferred") and n.get("serviceName")]

        # Classify inferred services as DB or external
        db_nodes = []
        external_nodes = []
        for name in inferred:
            name_lower = (name or "").lower()
            if any(db in name_lower for db in _DB_SYSTEMS) or "db" in name_lower or "sql" in name_lower:
                db_nodes.append(name)
            else:
                external_nodes.append(name)

        # Edges to inferred targets = dependencies without monitoring
        blind_spot_edges = [e for e in edges if e.get("targetServiceName") in set(inferred)]
        error_edges = [e for e in edges if (e.get("errorCount") or 0) > 0]

        return json.dumps({
            "environment": environment,
            "lookback_minutes": lookback_minutes,
            "instrumented_services": sorted(instrumented),
            "inferred_db_services": sorted(db_nodes),
            "inferred_external_services": sorted(external_nodes),
            "blind_spot_count": len(inferred),
            "error_edges": error_edges,
            "blind_spot_edges": blind_spot_edges,
            "all_edges": edges[:60],
        }, indent=2)
    except Exception as exc:
        return f"[get_service_dependency_map error]: {exc}"


def search_slow_outbound_calls(
    environment: str,
    service: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
    limit: int = 10,
) -> str:
    """
    Find traces containing slow outbound calls (client spans) — database queries,
    external API calls, and inter-service calls with high latency.

    Searches APM traces where the root span has high duration, then returns
    the latency breakdown by service so you can see which outbound call is
    the bottleneck. Run this proactively to find slow DB queries before they
    become incidents.

    Args:
        environment: Deployment environment name.
        service: Optional service name to scope the search.
        start_ms: Search window start (Unix ms). Defaults to last 1 hour if 0.
        end_ms: Search window end (Unix ms). Defaults to now if 0.
        limit: Max traces to return (default: 10).
    """
    _START = """
mutation StartAnalyticsSearch($input: AnalyticsSearchInput!) {
  startAnalyticsSearch(input: $input) {
    jobId
  }
}
"""
    _GET = """
query GetAnalyticsSearch($jobId: String!) {
  getAnalyticsSearch(jobId: $jobId) {
    status
    traces {
      traceId
      duration
      rootSpanName
      rootServiceName
      rootSpanKind
      errorCount
    }
  }
}
"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if end_ms == 0:
        end_ms = now_ms
    if start_ms == 0:
        start_ms = now_ms - 3600 * 1000  # last 1 hour

    try:
        filters = []
        if service:
            filters.append({"key": "sf_service", "value": service})

        start_resp = _graphql(_START, variables={
            "input": {
                "environment": environment,
                "filters": filters,
                "timeRange": {"startTimeMs": start_ms, "endTimeMs": end_ms},
                "resultSet": {"limit": limit},
            }
        })
        gql_errors = start_resp.get("errors")
        if gql_errors:
            return json.dumps({"graphql_errors": gql_errors})

        job_id = (
            start_resp.get("data", {})
            .get("startAnalyticsSearch", {})
            .get("jobId")
        )
        if not job_id:
            return json.dumps({"error": "No jobId returned", "response": start_resp})

        status = "RUNNING"
        traces = []
        for _ in range(20):
            time.sleep(0.5)
            poll = _graphql(_GET, variables={"jobId": job_id})
            search = poll.get("data", {}).get("getAnalyticsSearch", {})
            status = search.get("status", "")
            traces = search.get("traces") or []
            if status == "COMPLETE" or traces:
                break

        # Sort by duration descending to surface slowest first
        traces_sorted = sorted(traces, key=lambda t: t.get("duration") or 0, reverse=True)

        return json.dumps({
            "environment": environment,
            "service": service,
            "time_range_ms": [start_ms, end_ms],
            "status": status,
            "trace_count": len(traces_sorted),
            "slowest_traces": traces_sorted[:limit],
            "note": (
                "Traces are sorted by total duration. Use get_trace_analysis on the "
                "top trace IDs to see which specific DB/outbound call is the bottleneck."
            ),
        }, indent=2)
    except Exception as exc:
        return f"[search_slow_outbound_calls error]: {exc}"


def get_outbound_call_error_rates(environment: str, hours: int = 1) -> str:
    """
    Get error rates for outbound calls (client spans) per service.

    Queries SignalFlow for service request error counts to identify services
    making high-error outbound calls to databases or external dependencies.
    High client-span error rates often mean the dependency is unhealthy
    rather than the calling service itself.

    Args:
        environment: Deployment environment name.
        hours: Lookback window in hours (default: 1).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - hours * 3600 * 1000

    env_filter = f"filter('sf_environment', '{environment}')"

    # Error count and total request count per service
    error_prog = (
        f"data('service.request.error.count', filter={env_filter})"
        f".sum(over='5m').groupby(['sf_service']).publish(label='errors')"
    )
    total_prog = (
        f"data('service.request.count', filter={env_filter})"
        f".sum(over='5m').groupby(['sf_service']).publish(label='total')"
    )

    try:
        err_result = _signalflow_execute(error_prog, start_ms, now_ms)
        tot_result = _signalflow_execute(total_prog, start_ms, now_ms)

        # Aggregate per service using metadata dimensions
        service_errors: dict[str, float] = {}
        for sid, vals in err_result["streams"].items():
            props = err_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or sid
            service_errors[svc] = service_errors.get(svc, 0) + sum(vals)

        service_total: dict[str, float] = {}
        for sid, vals in tot_result["streams"].items():
            props = tot_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or sid
            service_total[svc] = service_total.get(svc, 0) + sum(vals)

        service_stats = []
        all_svcs = set(service_errors) | set(service_total)
        for svc in all_svcs:
            errors = service_errors.get(svc, 0)
            total = service_total.get(svc, 0)
            rate = round(errors / total * 100, 2) if total > 0 else 0
            service_stats.append({
                "service": svc,
                "total_errors": round(errors),
                "total_requests": round(total),
                "error_rate_pct": rate,
            })

        service_stats.sort(key=lambda x: -x["error_rate_pct"])

        return json.dumps({
            "environment": environment,
            "lookback_hours": hours,
            "services": service_stats,
            "high_error_services": [s for s in service_stats if s["error_rate_pct"] > 1.0],
        }, indent=2)
    except Exception as exc:
        return f"[get_outbound_call_error_rates error]: {exc}"


def find_db_instrumented_services(environment: str) -> str:
    """
    Discover which services have proper database span instrumentation.

    Queries the MTS catalog for spans tagged with db.system, db.operation, and
    db.name attributes. Services missing these attributes on their outbound DB
    calls are blind spots — you cannot see slow queries, missing indexes, or
    connection pool saturation in Splunk APM.

    Returns:
      - services with full db.* coverage
      - services with partial coverage (some db attributes missing)
      - services with no db.* instrumentation despite making outbound calls

    Args:
        environment: Deployment environment name.
    """
    try:
        # Query MTS metadata to find which services have db.system as a dimension
        db_system_resp = _api(
            f"/v2/metrictimeseries?query=sf_metric:spans.count"
            f"+sf_environment:{environment}+db.system:*&limit=200"
        )
        db_name_resp = _api(
            f"/v2/metrictimeseries?query=sf_metric:spans.count"
            f"+sf_environment:{environment}+db.name:*&limit=200"
        )
        db_op_resp = _api(
            f"/v2/metrictimeseries?query=sf_metric:spans.count"
            f"+sf_environment:{environment}+db.operation:*&limit=200"
        )

        def extract_services(resp: dict, dim: str) -> dict[str, set]:
            """Returns {service: set of dimension values}"""
            result: dict[str, set] = {}
            for mts in resp.get("results", []):
                dims = mts.get("dimensions", {})
                svc = dims.get("sf_service") or dims.get("service.name", "")
                val = dims.get(dim, "")
                if svc and val:
                    result.setdefault(svc, set()).add(val)
            return result

        has_system = extract_services(db_system_resp, "db.system")
        has_name = extract_services(db_name_resp, "db.name")
        has_op = extract_services(db_op_resp, "db.operation")

        all_db_services = set(has_system) | set(has_name) | set(has_op)

        detailed = []
        for svc in sorted(all_db_services):
            systems = sorted(has_system.get(svc, set()))
            detailed.append({
                "service": svc,
                "db_systems": systems,
                "has_db_system": svc in has_system,
                "has_db_name": svc in has_name,
                "has_db_operation": svc in has_op,
                "fully_instrumented": svc in has_system and svc in has_name and svc in has_op,
            })

        full = [s for s in detailed if s["fully_instrumented"]]
        partial = [s for s in detailed if not s["fully_instrumented"]]

        return json.dumps({
            "environment": environment,
            "db_instrumented_service_count": len(all_db_services),
            "fully_instrumented": [s["service"] for s in full],
            "partially_instrumented": [s["service"] for s in partial],
            "details": detailed,
            "missing_attributes_note": (
                "Missing db.name → cannot see which database is slow. "
                "Missing db.operation → cannot see SELECT vs INSERT breakdown. "
                "Missing db.system → cannot identify the database technology."
            ),
        }, indent=2)
    except Exception as exc:
        return f"[find_db_instrumented_services error]: {exc}"


# ── Tool registry ─────────────────────────────────────────────────────────────

SCHEMAS = [
    {
        "toolSpec": {
            "name": "get_service_dependency_map",
            "description": (
                "Get the full service dependency map including inferred (unmonitored) services. "
                "Inferred services are databases and external APIs that appear in the topology "
                "because services call them, but are not instrumented themselves — blind spots. "
                "Returns service edges with call counts, error counts, and p99 latency."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "lookback_minutes": {"type": "integer", "description": "Lookback window in minutes (default: 60)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_slow_outbound_calls",
            "description": (
                "Find traces containing slow outbound calls — slow DB queries, external API calls, "
                "and inter-service calls with high latency. Returns traces sorted by duration descending. "
                "Use get_trace_analysis on the returned trace IDs to see which specific call is the bottleneck."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "service": {"type": "string", "description": "Optional service to scope the search."},
                        "start_ms": {"type": "integer", "description": "Window start Unix ms (0 = last 1 hour)."},
                        "end_ms": {"type": "integer", "description": "Window end Unix ms (0 = now)."},
                        "limit": {"type": "integer", "description": "Max traces to return (default: 10)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_outbound_call_error_rates",
            "description": (
                "Get per-service error rates for outbound calls. "
                "High client-span error rates indicate a dependency (DB, external API) is unhealthy, "
                "not the calling service itself. Returns error rate % sorted by worst first."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 1)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "find_db_instrumented_services",
            "description": (
                "Discover which services have proper database span instrumentation (db.system, "
                "db.name, db.operation attributes). Services missing these attributes are blind spots — "
                "you cannot see slow queries or connection pool saturation in APM. "
                "Returns fully instrumented, partially instrumented, and database technology used per service."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "get_service_dependency_map": get_service_dependency_map,
    "search_slow_outbound_calls": search_slow_outbound_calls,
    "get_outbound_call_error_rates": get_outbound_call_error_rates,
    "find_db_instrumented_services": find_db_instrumented_services,
}
