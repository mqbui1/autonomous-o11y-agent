"""
RCA (Root Cause Analysis) tools for Splunk Observability Cloud.

Gathers correlated cross-signal data for incident investigation:
- Active incidents from Splunk Observability detectors
- Error/slow traces via APM GraphQL async search
- Latency contributors via trace analysis
- Service topology and dependency graph
- Deployment/change events via REST events API
- Error rate and latency time series via SignalFlow
- Infrastructure metrics (CPU, memory) via SignalFlow

API patterns follow the Splunk Observability Cloud REST + APM GraphQL + SignalFlow APIs,
modeled on the o11y-mcp server tooling capabilities.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ._runner import get_config

logger = logging.getLogger(__name__)


# ── Low-level API helpers ─────────────────────────────────────────────────────

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
    """Execute an APM GraphQL query against /v2/apm/graphql."""
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
    """Execute a SignalFlow program over a time range.

    The API returns SSE (Server-Sent Events): multi-line events with
    `event: <type>` and `data: <json_fragment>` prefixes, blank-line separated.
    data events: {"data": [{"tsId": "<id>", "value": <float>}], ...}
    metadata events: {"tsId": "<id>", "properties": {...}}

    Returns {streams: {tsId: [values]}, metadata: {tsId: {properties}}}.
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


def _flatten_stream_values(result: dict) -> list[float]:
    """Flatten all stream values from a SignalFlow result into a single list."""
    vals = []
    for v in result["streams"].values():
        vals.extend(v)
    return vals


# ── RCA Tool Functions ────────────────────────────────────────────────────────

def get_active_incidents(environment: str = "") -> str:
    """
    List active incidents from Splunk Observability Cloud detectors.

    Returns all currently firing alerts with detector name, severity, affected
    services/dimensions, and trigger time. Use this as the starting point for
    incident investigation to understand what is currently alerting.

    Args:
        environment: Filter to incidents whose detector name contains this string.
                     Leave empty to return all active incidents.
    """
    try:
        data = _api("/v2/incident?includeResolved=false&limit=50")
        # API returns a list directly (not wrapped in {"results": [...]})
        incidents = data if isinstance(data, list) else data.get("results", [])
        if environment:
            env_lower = environment.lower()
            incidents = [
                i for i in incidents
                if env_lower in str(i.get("detectorName", "")).lower()
                or env_lower in json.dumps(i.get("inputs", {})).lower()
            ]
        trimmed = []
        for inc in incidents:
            trimmed.append({
                "incidentId": inc.get("incidentId"),
                "severity": inc.get("severity"),
                "detectorName": inc.get("detectorName"),
                "detectorId": inc.get("detectorId"),
                "anomalyState": inc.get("anomalyState"),
                "triggeredAt": inc.get("triggeredAt"),
                "inputs": inc.get("inputs", {}),
            })
        return json.dumps({"count": len(trimmed), "incidents": trimmed}, indent=2)
    except Exception as exc:
        return f"[get_active_incidents error]: {exc}"


def search_error_traces(
    service: str,
    environment: str,
    start_ms: int,
    end_ms: int,
    limit: int = 10,
) -> str:
    """
    Search for services with errors near the incident time using SignalFlow metrics.

    Returns error counts, error rates, and top erroring operations per service.
    The APM async trace search API (startAnalyticsSearch) was removed from the
    Splunk GraphQL schema; this implementation uses span error metrics instead,
    which provide the same triage signal.

    Args:
        service: Service name to filter (e.g. 'frontend'). Empty = all services.
        environment: Deployment environment (e.g. 'production').
        start_ms: Search window start in Unix milliseconds.
        end_ms: Search window end in Unix milliseconds.
        limit: Maximum number of services to return (default: 10).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if end_ms == 0:
        end_ms = now_ms
    if start_ms == 0:
        start_ms = now_ms - 3600 * 1000

    env_filter = f"filter('sf_environment', '{environment}')"
    if service:
        env_filter += f" and filter('sf_service', '{service}')"

    try:
        err_prog = (
            f"data('spans.count', filter={env_filter} and filter('sf_error', 'true'))"
            f".sum(by=['sf_service', 'sf_operation']).publish(label='errors')"
        )
        tot_prog = (
            f"data('spans.count', filter={env_filter})"
            f".sum(by=['sf_service', 'sf_operation']).publish(label='total')"
        )
        err_result = _signalflow_execute(err_prog, start_ms, end_ms)
        tot_result = _signalflow_execute(tot_prog, start_ms, end_ms)

        # Aggregate by service+operation from metadata dimensions
        err_by_op: dict[tuple, float] = {}
        for tsid, vals in err_result["streams"].items():
            meta = err_result["metadata"].get(tsid, {})
            svc = meta.get("sf_service", "unknown")
            op = meta.get("sf_operation", "")
            key = (svc, op)
            err_by_op[key] = err_by_op.get(key, 0) + sum(vals)

        tot_by_op: dict[tuple, float] = {}
        for tsid, vals in tot_result["streams"].items():
            meta = tot_result["metadata"].get(tsid, {})
            svc = meta.get("sf_service", "unknown")
            op = meta.get("sf_operation", "")
            key = (svc, op)
            tot_by_op[key] = tot_by_op.get(key, 0) + sum(vals)

        all_keys = set(err_by_op) | set(tot_by_op)
        rows = []
        for (svc, op) in all_keys:
            errs = err_by_op.get((svc, op), 0)
            total = tot_by_op.get((svc, op), 0)
            if errs == 0 and total == 0:
                continue
            rate = round(errs / total * 100, 1) if total > 0 else 100.0
            rows.append({
                "service": svc,
                "operation": op,
                "error_count": round(errs),
                "total_count": round(total),
                "error_rate_pct": rate,
            })

        rows.sort(key=lambda r: r["error_count"], reverse=True)
        return json.dumps({
            "service_filter": service,
            "environment": environment,
            "time_range_ms": [start_ms, end_ms],
            "erroring_operations": rows[:limit],
            "total_erroring_ops": len(rows),
        }, indent=2)
    except Exception as exc:
        return f"[search_error_traces error]: {exc}"


def get_trace_analysis(trace_id: str) -> str:
    """
    Get latency contributors and error contributors for a specific trace.

    Returns a ranked list of service+operation pairs sorted by their percentage of
    total trace duration. Use this to pinpoint which service/call is causing slowness.
    Also surfaces which services contributed errors to the trace.

    Args:
        trace_id: The trace ID to analyze (obtained from search_error_traces).
    """
    _ANALYSIS = """
query GetTraceAnalysis($traceId: ID!) {
  getTraceAnalysis(traceId: $traceId) {
    topLatencyContributors {
      serviceName
      operationName
      totalTime
      percentOfTrace
    }
    errorContributors {
      serviceName
      operationName
      errorCount
    }
  }
}
"""
    try:
        resp = _graphql(_ANALYSIS, variables={"traceId": trace_id})
        errors = resp.get("errors")
        if errors:
            # getTraceAnalysis was removed; return structured error with hint
            return json.dumps({
                "traceId": trace_id,
                "note": "getTraceAnalysis is no longer available in the APM GraphQL schema. "
                        "Use search_error_traces to identify erroring services/operations instead.",
                "graphql_errors": errors,
            }, indent=2)
        analysis = resp.get("data", {}).get("getTraceAnalysis", {})
        return json.dumps({
            "traceId": trace_id,
            "topLatencyContributors": analysis.get("topLatencyContributors", []),
            "errorContributors": analysis.get("errorContributors", []),
        }, indent=2)
    except Exception as exc:
        return f"[get_trace_analysis error]: {exc}"


def get_service_topology(environment: str, lookback_minutes: int = 60) -> str:
    """
    Get active services and their error/latency profile for an environment.

    Returns all services emitting spans, with call counts, error counts, error rates,
    and p99 latency. The getServiceMap GraphQL API was removed from the Splunk schema;
    this implementation uses SignalFlow span metrics which provide the same service-level
    visibility (individual service health, not inter-service edges).

    Args:
        environment: Deployment environment name.
        lookback_minutes: How far back to look (default: 60).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000
    env_filter = f"filter('sf_environment', '{environment}')"

    try:
        err_prog = (
            f"data('spans.count', filter={env_filter} and filter('sf_error', 'true'))"
            f".sum(by=['sf_service']).publish()"
        )
        tot_prog = (
            f"data('spans.count', filter={env_filter})"
            f".sum(by=['sf_service']).publish()"
        )
        p99_prog = (
            f"data('service.request.duration.ns.p99', filter={env_filter})"
            f".max(by=['sf_service']).publish()"
        )
        err_r = _signalflow_execute(err_prog, start_ms, now_ms)
        tot_r = _signalflow_execute(tot_prog, start_ms, now_ms)
        p99_r = _signalflow_execute(p99_prog, start_ms, now_ms)

        def _agg(result, fn=sum):
            out: dict[str, float] = {}
            for tsid, vals in result["streams"].items():
                svc = result["metadata"].get(tsid, {}).get("sf_service", "unknown")
                out[svc] = fn(out.get(svc, 0), fn(vals)) if vals else out.get(svc, 0)
            return out

        err_by_svc = _agg(err_r)
        tot_by_svc = _agg(tot_r)
        p99_by_svc = _agg(p99_r, fn=max)

        all_svcs = set(err_by_svc) | set(tot_by_svc)
        services = []
        for svc in sorted(all_svcs):
            errs = round(err_by_svc.get(svc, 0))
            total = round(tot_by_svc.get(svc, 0))
            rate = round(errs / total * 100, 1) if total > 0 else 0.0
            p99_ns = p99_by_svc.get(svc)
            p99_ms = round(p99_ns / 1e6, 1) if p99_ns else None
            services.append({
                "serviceName": svc,
                "callCount": total,
                "errorCount": errs,
                "errorRatePct": rate,
                "p99LatencyMs": p99_ms,
            })

        error_services = [s for s in services if s["errorCount"] > 0]
        return json.dumps({
            "environment": environment,
            "lookback_minutes": lookback_minutes,
            "service_count": len(services),
            "services": services,
            "error_services": error_services,
        }, indent=2)
    except Exception as exc:
        return f"[get_service_topology error]: {exc}"


def find_change_events(environment: str, start_ms: int, end_ms: int) -> str:
    """
    Find deployment and configuration change events in a time window.

    Searches Splunk Observability event store for deployment events, config changes,
    and other operational events. Correlate these timestamps with the incident start
    time — a deployment minutes before an error spike is a strong causal signal.

    Args:
        environment: Deployment environment name.
        start_ms: Window start in Unix milliseconds.
        end_ms: Window end in Unix milliseconds.
    """
    try:
        # Primary: query events with environment filter
        payload = {
            "query": f"deployment.environment:{environment}",
            "startTime": start_ms,
            "endTime": end_ms,
            "orderBy": "-timestamp",
            "limit": 50,
        }
        try:
            data = _api("/v2/event/find", payload=payload, method="POST")
            events = data.get("results", [])
        except Exception:
            events = []

        # Fallback: broader query filtered client-side
        if not events:
            try:
                payload2 = {
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "orderBy": "-timestamp",
                    "limit": 100,
                }
                data2 = _api("/v2/event/find", payload=payload2, method="POST")
                env_lower = environment.lower()
                events = [
                    e for e in data2.get("results", [])
                    if env_lower in json.dumps(e).lower()
                ]
            except Exception:
                pass

        trimmed = []
        for ev in events:
            dims = ev.get("dimensions", {})
            props = ev.get("properties", {})
            trimmed.append({
                "timestamp_ms": ev.get("timestamp"),
                "eventType": ev.get("eventType") or ev.get("category", ""),
                "service": dims.get("service") or dims.get("sf_service") or props.get("service", ""),
                "description": props.get("description") or props.get("summary", ""),
                "dimensions": dims,
            })

        trimmed.sort(key=lambda e: e.get("timestamp_ms") or 0)
        return json.dumps({
            "environment": environment,
            "time_range_ms": [start_ms, end_ms],
            "event_count": len(trimmed),
            "events": trimmed,
        }, indent=2)
    except Exception as exc:
        return f"[find_change_events error]: {exc}"


def get_service_error_rate(service: str = "", environment: str = "", hours: int = 1) -> str:
    """
    Get error rate statistics for a service over a time window.

    Returns total errors, total requests, computed error rate %, and peak error count
    per minute. Use this to establish the exact timeline of when the error rate spiked.

    Args:
        service: Service name (e.g. 'checkout-service').
        environment: Deployment environment name.
        hours: Lookback window in hours (default: 1).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    svc_filter = (
        f"filter('sf_environment', '{environment}') and filter('sf_service', '{service}')"
    )
    error_prog = (
        f"data('spans.count', filter={svc_filter} and filter('sf_error', 'true'))"
        f".sum(over='1m').publish(label='errors')"
    )
    total_prog = (
        f"data('spans.count', filter={svc_filter})"
        f".sum(over='1m').publish(label='total')"
    )
    try:
        err_result = _signalflow_execute(error_prog, start_ms, now_ms)
        tot_result = _signalflow_execute(total_prog, start_ms, now_ms)
        err_vals = _flatten_stream_values(err_result)
        tot_vals = _flatten_stream_values(tot_result)

        total_errors = sum(err_vals)
        total_requests = sum(tot_vals)
        error_rate_pct = (total_errors / total_requests * 100) if total_requests > 0 else 0
        peak_errors = max(err_vals) if err_vals else 0

        return json.dumps({
            "service": service,
            "environment": environment,
            "lookback_hours": hours,
            "total_errors": round(total_errors),
            "total_requests": round(total_requests),
            "error_rate_pct": round(error_rate_pct, 2),
            "peak_error_count_per_minute": round(peak_errors),
            "data_points": len(err_vals),
        }, indent=2)
    except Exception as exc:
        return f"[get_service_error_rate error]: {exc}"


def get_service_latency(service: str, environment: str, hours: int = 1) -> str:
    """
    Get p99 latency statistics for a service over a time window.

    Returns average, peak, and latest p99 latency in milliseconds. Use this alongside
    get_service_error_rate to distinguish between high-error incidents (service broken)
    and high-latency incidents (service slow, possibly due to a dependency).

    Args:
        service: Service name.
        environment: Deployment environment name.
        hours: Lookback window in hours (default: 1).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    svc_filter = (
        f"filter('sf_environment', '{environment}') and filter('sf_service', '{service}')"
    )
    # Splunk APM p99 metric is in nanoseconds
    p99_prog = (
        f"data('service.request.duration.p99', filter={svc_filter})"
        f".publish(label='p99_ns')"
    )
    try:
        result = _signalflow_execute(p99_prog, start_ms, now_ms)
        all_vals = _flatten_stream_values(result)
        if not all_vals:
            return json.dumps({
                "service": service,
                "environment": environment,
                "note": "No p99 latency data found — service may not report this metric",
            })
        avg_ns = sum(all_vals) / len(all_vals)
        peak_ns = max(all_vals)
        latest_ns = all_vals[-1]
        return json.dumps({
            "service": service,
            "environment": environment,
            "lookback_hours": hours,
            "p99_avg_ms": round(avg_ns / 1_000_000, 1),
            "p99_peak_ms": round(peak_ns / 1_000_000, 1),
            "p99_latest_ms": round(latest_ns / 1_000_000, 1),
            "data_points": len(all_vals),
        }, indent=2)
    except Exception as exc:
        return f"[get_service_latency error]: {exc}"


def get_infra_metrics(environment: str, service: str = "", hours: int = 1) -> str:
    """
    Get infrastructure metrics (CPU, memory) for pods/hosts running a service.

    High CPU or memory saturation on the pods running a service can be the root cause
    of latency spikes and errors. Queries Kubernetes pod metrics first, falls back to
    host-level metrics.

    Args:
        environment: Deployment environment name.
        service: Optional service name to scope to pods for that service.
        hours: Lookback window in hours (default: 1).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - hours * 3600 * 1000

    base_filter = f"filter('sf_environment', '{environment}')"
    if service:
        base_filter = (
            f"filter('sf_environment', '{environment}') and "
            f"(filter('service.name', '{service}') or filter('k8s.deployment.name', '{service}'))"
        )

    results = {}

    # Try k8s pod-level metrics first
    cpu_prog = (
        f"data('k8s.pod.cpu.utilization', filter={base_filter})"
        f".mean(over='5m').publish(label='cpu')"
    )
    mem_prog = (
        f"data('k8s.pod.memory.usage', filter={base_filter})"
        f".mean(over='5m').publish(label='mem')"
    )
    try:
        cpu_result = _signalflow_execute(cpu_prog, start_ms, now_ms)
        cpu_vals = _flatten_stream_values(cpu_result)
        if cpu_vals:
            results["k8s_cpu"] = {
                "avg_pct": round(sum(cpu_vals) / len(cpu_vals) * 100, 1),
                "peak_pct": round(max(cpu_vals) * 100, 1),
                "data_points": len(cpu_vals),
            }
    except Exception as exc:
        logger.debug("k8s cpu query failed: %s", exc)

    try:
        mem_result = _signalflow_execute(mem_prog, start_ms, now_ms)
        mem_vals = _flatten_stream_values(mem_result)
        if mem_vals:
            results["k8s_memory_mb"] = {
                "avg_mb": round(sum(mem_vals) / len(mem_vals) / (1024 * 1024), 1),
                "peak_mb": round(max(mem_vals) / (1024 * 1024), 1),
                "data_points": len(mem_vals),
            }
    except Exception as exc:
        logger.debug("k8s memory query failed: %s", exc)

    # Host-level CPU fallback
    if "k8s_cpu" not in results:
        host_filter = f"filter('sf_environment', '{environment}')"
        host_cpu_prog = (
            f"data('cpu.utilization', filter={host_filter})"
            f".mean(over='5m').publish(label='host_cpu')"
        )
        try:
            hcpu_result = _signalflow_execute(host_cpu_prog, start_ms, now_ms)
            hcpu_vals = _flatten_stream_values(hcpu_result)
            if hcpu_vals:
                results["host_cpu_pct"] = {
                    "avg_pct": round(sum(hcpu_vals) / len(hcpu_vals), 1),
                    "peak_pct": round(max(hcpu_vals), 1),
                    "data_points": len(hcpu_vals),
                }
        except Exception as exc:
            logger.debug("host cpu query failed: %s", exc)

    return json.dumps({
        "environment": environment,
        "service": service,
        "lookback_hours": hours,
        "metrics": results,
        "note": (
            "k8s_cpu values are percent (0-100). k8s_memory in MB. "
            "Empty metrics means no data found for this service/environment."
        ),
    }, indent=2)


# ── Tool registry ─────────────────────────────────────────────────────────────

SCHEMAS = [
    {
        "toolSpec": {
            "name": "get_active_incidents",
            "description": (
                "List active incidents from Splunk Observability Cloud detectors. "
                "Returns all currently firing alerts with detector name, severity, "
                "affected dimensions, and trigger time. Use as the starting point "
                "for incident investigation."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "environment": {
                            "type": "string",
                            "description": "Filter to incidents whose detector name contains this string. Leave empty for all active incidents.",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_error_traces",
            "description": (
                "Search for traces with errors near the incident time using APM async search. "
                "Returns top error traces with trace IDs, duration, root service, and error count. "
                "Use returned trace IDs with get_trace_analysis to pinpoint the failing operation."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["service", "environment", "start_ms", "end_ms"],
                    "properties": {
                        "service": {"type": "string", "description": "Service name to search."},
                        "environment": {"type": "string", "description": "Deployment environment."},
                        "start_ms": {"type": "integer", "description": "Window start in Unix milliseconds."},
                        "end_ms": {"type": "integer", "description": "Window end in Unix milliseconds."},
                        "limit": {"type": "integer", "description": "Max traces to return (default: 10)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_trace_analysis",
            "description": (
                "Get latency contributors and error contributors for a specific trace. "
                "Returns service+operation pairs ranked by their percentage of total trace duration. "
                "Use this to pinpoint which service/call is causing the slowness or errors."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["trace_id"],
                    "properties": {
                        "trace_id": {"type": "string", "description": "Trace ID from search_error_traces."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_service_topology",
            "description": (
                "Get the service dependency graph for an environment. "
                "Returns service-to-service call edges with call counts, error counts, and p99 latency. "
                "Use to understand blast radius: which upstream callers are affected, "
                "and which downstream dependencies the failing service depends on."
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
            "name": "find_change_events",
            "description": (
                "Find deployment and configuration change events in a time window. "
                "A deployment event minutes before an error spike is a strong causal signal. "
                "Returns event type, service, description, and timestamp."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment", "start_ms", "end_ms"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "start_ms": {"type": "integer", "description": "Window start in Unix milliseconds."},
                        "end_ms": {"type": "integer", "description": "Window end in Unix milliseconds."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_service_error_rate",
            "description": (
                "Get error rate statistics for a service. Returns total errors, total requests, "
                "computed error rate %, and peak error count per minute. "
                "Use to establish the exact moment error rate spiked and its magnitude."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["service", "environment"],
                    "properties": {
                        "service": {"type": "string", "description": "Service name."},
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 1)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_service_latency",
            "description": (
                "Get p99 latency statistics for a service. Returns average, peak, and latest p99 in ms. "
                "Use alongside get_service_error_rate to distinguish high-error incidents "
                "(service broken) from high-latency incidents (service slow, dependency degraded)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["service", "environment"],
                    "properties": {
                        "service": {"type": "string", "description": "Service name."},
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 1)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_infra_metrics",
            "description": (
                "Get infrastructure metrics (CPU, memory) for pods/hosts running a service. "
                "High CPU or memory saturation can be the root cause of latency spikes and errors. "
                "Queries Kubernetes pod metrics first, falls back to host-level metrics."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string", "description": "Deployment environment name."},
                        "service": {"type": "string", "description": "Optional service name to scope to specific pods."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 1)."},
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "get_active_incidents": get_active_incidents,
    "search_error_traces": search_error_traces,
    "get_trace_analysis": get_trace_analysis,
    "get_service_topology": get_service_topology,
    "find_change_events": find_change_events,
    "get_service_error_rate": get_service_error_rate,
    "get_service_latency": get_service_latency,
    "get_infra_metrics": get_infra_metrics,
}
