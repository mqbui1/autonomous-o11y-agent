"""
Database and dependency analysis tools for Splunk Observability Cloud.

Proactively surfaces slow database operations, unhealthy external dependencies,
and instrumentation gaps in outbound call coverage:

- Service dependency topology with inferred service nodes (DBs, external APIs)
- Slow DB and outbound HTTP operations ranked by p99 latency via SignalFlow
- Per-service outbound error rates via SignalFlow
- Discovery of which services have db.* span attributes (DB instrumentation quality)

Uses SignalFlow REST APIs — no subprocess dependencies.
Note: getServiceMap and startAnalyticsSearch were removed from the APM GraphQL schema
and have been replaced with equivalent SignalFlow metric queries.
"""

import json
import logging
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
    """Execute a SignalFlow program and return {streams, metadata}.

    The API returns SSE (Server-Sent Events): each event spans multiple lines
    with `event: <type>` and `data: <json_fragment>` prefixes, separated by
    blank lines. We must accumulate data: lines into a complete JSON body before
    parsing, rather than trying to JSON-parse each line individually.

    data events use the format:
      {"data": [{"tsId": "<id>", "value": <float>}], "logicalTimestampMs": ...}
    metadata events use:
      {"tsId": "<id>", "properties": {...}}
    """
    cfg = get_config()
    qs = f"?start={start_ms}&stop={end_ms}&resolution=60000&immediate=true"
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute{qs}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "text/plain"}
    req = urllib.request.Request(
        url, data=program.encode("utf-8"), headers=headers, method="POST"
    )
    streams: dict[str, list] = {}
    metadata: dict[str, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            current_event_type = None
            data_lines: list[str] = []
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("event: "):
                    current_event_type = line[7:].strip()
                    data_lines = []
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and data_lines:
                    # Blank line = end of SSE event; parse accumulated data
                    try:
                        msg = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
                        continue
                    etype = current_event_type or msg.get("type", "")
                    if etype == "metadata":
                        tsid = msg.get("tsId", "")
                        if tsid:
                            metadata[tsid] = msg.get("properties", {})
                    elif etype == "data":
                        for point in msg.get("data", []):
                            tsid = point.get("tsId", "")
                            val = point.get("value")
                            if tsid and val is not None:
                                streams.setdefault(tsid, []).append(float(val))
                    data_lines = []
    except Exception as exc:
        logger.warning("SignalFlow execute failed: %s", exc)
    return {"streams": streams, "metadata": metadata}


# ── DB / Dependency Tool Functions ────────────────────────────────────────────

def get_service_dependency_map(environment: str, lookback_minutes: int = 60) -> str:
    """
    Get the active service list with per-service call counts, error rates, and p99 latency.

    Note: getServiceMap (topology edges + inferred DB/external services) was removed from
    the Splunk APM GraphQL schema. This function now uses SignalFlow to surface active
    instrumented services with their request metrics as the nearest equivalent.

    Args:
        environment: Deployment environment name.
        lookback_minutes: How far back to look (default: 60).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000

    env_filter = f"filter('sf_environment', '{environment}')"

    req_prog = (
        f"data('spans.count', filter={env_filter})"
        f".sum(by=['sf_service']).publish(label='reqs')"
    )
    err_prog = (
        f"data('spans.count', filter={env_filter} and filter('sf_error', 'true'))"
        f".sum(by=['sf_service']).publish(label='errors')"
    )
    p99_prog = (
        f"data('service.request.duration.ns.p99', filter={env_filter})"
        f".mean(by=['sf_service']).publish(label='p99')"
    )

    try:
        req_result = _signalflow_execute(req_prog, start_ms, now_ms)
        err_result = _signalflow_execute(err_prog, start_ms, now_ms)
        p99_result = _signalflow_execute(p99_prog, start_ms, now_ms)

        svc_reqs: dict[str, float] = {}
        for sid, vals in req_result["streams"].items():
            props = req_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or sid
            svc_reqs[svc] = svc_reqs.get(svc, 0) + sum(vals)

        svc_errors: dict[str, float] = {}
        for sid, vals in err_result["streams"].items():
            props = err_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or sid
            svc_errors[svc] = svc_errors.get(svc, 0) + sum(vals)

        svc_p99: dict[str, list] = {}
        for sid, vals in p99_result["streams"].items():
            props = p99_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or sid
            svc_p99.setdefault(svc, []).extend(vals)

        all_services = set(svc_reqs) | set(svc_errors)
        service_nodes = []
        for svc in sorted(all_services):
            requests = round(svc_reqs.get(svc, 0))
            errors = round(svc_errors.get(svc, 0))
            p99_vals = svc_p99.get(svc, [])
            p99_ms = round(max(p99_vals) / 1e6, 2) if p99_vals else None
            error_rate = round(errors / requests * 100, 2) if requests > 0 else 0
            service_nodes.append({
                "service": svc,
                "total_requests": requests,
                "total_errors": errors,
                "error_rate_pct": error_rate,
                "p99_ms": p99_ms,
            })

        service_nodes.sort(key=lambda x: -x["total_requests"])
        error_services = [s for s in service_nodes if s["error_rate_pct"] > 0]

        return json.dumps({
            "environment": environment,
            "lookback_minutes": lookback_minutes,
            "active_services": [s["service"] for s in service_nodes],
            "service_count": len(service_nodes),
            "service_metrics": service_nodes,
            "error_services": error_services,
            "note": (
                "getServiceMap GraphQL was removed from the Splunk APM schema. "
                "Topology edges and inferred (DB/external) service detection are no longer "
                "available via API. Showing active instrumented services from SignalFlow metrics."
            ),
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
    Find slow outbound operations (DB queries, external API calls, inter-service calls)
    ranked by p99 latency.

    Note: startAnalyticsSearch/AnalyticsSearchInput were removed from the APM GraphQL
    schema. This function now uses SignalFlow p99 latency grouped by service+operation
    as the nearest equivalent, returning the slowest operations rather than individual traces.

    Args:
        environment: Deployment environment name.
        service: Optional service name to scope the search.
        start_ms: Search window start (Unix ms). Defaults to last 1 hour if 0.
        end_ms: Search window end (Unix ms). Defaults to now if 0.
        limit: Max operations to return (default: 10).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if end_ms == 0:
        end_ms = now_ms
    if start_ms == 0:
        start_ms = now_ms - 3600 * 1000  # last 1 hour

    env_filter = f"filter('sf_environment', '{environment}')"
    svc_filter = f" and filter('sf_service', '{service}')" if service else ""
    combined_filter = env_filter + svc_filter

    p99_prog = (
        f"data('service.request.duration.ns.p99', filter={combined_filter})"
        f".mean(by=['sf_service', 'sf_operation']).publish(label='p99')"
    )
    req_prog = (
        f"data('spans.count', filter={combined_filter})"
        f".sum(by=['sf_service', 'sf_operation']).publish(label='reqs')"
    )
    err_prog = (
        f"data('spans.count', filter={combined_filter} and filter('sf_error', 'true'))"
        f".sum(by=['sf_service', 'sf_operation']).publish(label='errors')"
    )

    try:
        p99_result = _signalflow_execute(p99_prog, start_ms, end_ms)
        req_result = _signalflow_execute(req_prog, start_ms, end_ms)
        err_result = _signalflow_execute(err_prog, start_ms, end_ms)

        op_p99: dict[str, list] = {}
        for sid, vals in p99_result["streams"].items():
            props = p99_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or ""
            op = props.get("sf_operation") or props.get("span.name") or ""
            key = f"{svc}::{op}"
            op_p99.setdefault(key, []).extend(vals)

        op_reqs: dict[str, float] = {}
        for sid, vals in req_result["streams"].items():
            props = req_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or ""
            op = props.get("sf_operation") or props.get("span.name") or ""
            key = f"{svc}::{op}"
            op_reqs[key] = op_reqs.get(key, 0) + sum(vals)

        op_errors: dict[str, float] = {}
        for sid, vals in err_result["streams"].items():
            props = err_result["metadata"].get(sid, {})
            svc = props.get("sf_service") or props.get("service.name") or ""
            op = props.get("sf_operation") or props.get("span.name") or ""
            key = f"{svc}::{op}"
            op_errors[key] = op_errors.get(key, 0) + sum(vals)

        ops = []
        for key in set(op_p99) | set(op_reqs):
            svc, op = key.split("::", 1)
            p99_vals = op_p99.get(key, [])
            p99_ms = round(max(p99_vals) / 1e6, 2) if p99_vals else 0
            reqs = round(op_reqs.get(key, 0))
            errors = round(op_errors.get(key, 0))
            ops.append({
                "service": svc,
                "operation": op,
                "p99_ms": p99_ms,
                "total_requests": reqs,
                "total_errors": errors,
            })

        ops.sort(key=lambda x: -x["p99_ms"])

        return json.dumps({
            "environment": environment,
            "service": service,
            "time_range_ms": [start_ms, end_ms],
            "operation_count": len(ops),
            "slowest_operations": ops[:limit],
            "note": (
                "startAnalyticsSearch/AnalyticsSearchInput were removed from the APM GraphQL schema. "
                "Returning p99 latency per service+operation from SignalFlow instead of individual traces. "
                "Operations are sorted by p99 latency descending."
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
        f"data('spans.count', filter={env_filter} and filter('sf_error', 'true'))"
        f".sum(by=['sf_service']).publish(label='errors')"
    )
    total_prog = (
        f"data('spans.count', filter={env_filter})"
        f".sum(by=['sf_service']).publish(label='total')"
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
