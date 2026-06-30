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
    """
    Execute a SignalFlow program over a time range.
    Returns {streams: {tsId: [values]}, metadata: {tsId: {properties}}}.
    """
    cfg = get_config()
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    payload = {
        "program": program,
        "start": start_ms,
        "stop": end_ms,
        "resolution": 60000,
        "immediate": True,
    }
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
        incidents = data.get("results", [])
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
    Search for traces with errors near the incident time using APM async search.

    Returns the top error traces with trace IDs, duration, root service, and error
    count. Use the returned trace IDs with get_trace_analysis to pinpoint which
    operation is causing the slowness or errors.

    Args:
        service: Service name to search (e.g. 'frontend', 'checkout-service').
        environment: Deployment environment (e.g. 'production').
        start_ms: Search window start in Unix milliseconds.
        end_ms: Search window end in Unix milliseconds.
        limit: Maximum number of traces to return (default: 10).
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
    try:
        filters = [{"key": "error", "value": "true"}]
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
        errors = start_resp.get("errors")
        if errors:
            return json.dumps({"graphql_errors": errors})

        job_id = (
            start_resp.get("data", {})
            .get("startAnalyticsSearch", {})
            .get("jobId")
        )
        if not job_id:
            return json.dumps({"error": "No jobId returned", "response": start_resp})

        # Poll until complete (up to 20 tries, 0.5s apart = 10s max)
        status = "RUNNING"
        traces = []
        for _ in range(20):
            time.sleep(0.5)
            poll_resp = _graphql(_GET, variables={"jobId": job_id})
            search = poll_resp.get("data", {}).get("getAnalyticsSearch", {})
            status = search.get("status", "")
            traces = search.get("traces") or []
            if status == "COMPLETE" or traces:
                break

        return json.dumps({
            "service": service,
            "environment": environment,
            "time_range_ms": [start_ms, end_ms],
            "status": status,
            "trace_count": len(traces),
            "traces": traces,
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
            return json.dumps({"graphql_errors": errors, "traceId": trace_id})
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
    Get the service dependency graph (topology) for an environment.

    Returns all service-to-service call edges with call counts, error counts, and p99
    latency. Use this to understand blast radius: which upstream services call the
    failing service, and which downstream services it depends on.

    Args:
        environment: Deployment environment name.
        lookback_minutes: How far back to look for service interactions (default: 60).
    """
    _MAP = """
query GetServiceMap($environment: String!, $windowMs: Long) {
  getServiceMap(environment: $environment, windowMs: $windowMs) {
    nodes {
      serviceName
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
        resp = _graphql(_MAP, variables={
            "environment": environment,
            "windowMs": window_ms,
        })
        errors = resp.get("errors")
        if errors:
            return json.dumps({"graphql_errors": errors})
        svc_map = resp.get("data", {}).get("getServiceMap", {})
        nodes = [
            n.get("serviceName") for n in svc_map.get("nodes", [])
            if n.get("serviceName")
        ]
        edges = svc_map.get("edges", [])
        error_edges = [e for e in edges if (e.get("errorCount") or 0) > 0]
        return json.dumps({
            "environment": environment,
            "lookback_minutes": lookback_minutes,
            "service_count": len(nodes),
            "services": sorted(nodes),
            "error_edges": error_edges,
            "all_edges": edges[:60],  # cap for context size
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


def get_service_error_rate(service: str, environment: str, hours: int = 1) -> str:
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
        f"data('service.request.error.count', filter={svc_filter})"
        f".sum(over='1m').publish(label='errors')"
    )
    total_prog = (
        f"data('service.request.count', filter={svc_filter})"
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
