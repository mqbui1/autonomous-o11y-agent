"""
Splunk AlwaysOn Profiling + APM call-pattern analysis tools.

Two data sources:
1. Splunk AlwaysOn Profiling API — CPU flame graphs, memory allocation stacks,
   thread states. Requires AlwaysOn Profiling to be enabled in the OTel Java/Python
   agent (SPLUNK_PROFILER_ENABLED=true). Returns file:line:function with CPU sample counts.

2. APM span pattern analysis — constructed from SignalFlow span metrics.
   Does NOT require profiling to be enabled. Detects N+1 queries, high-frequency
   outbound calls, and operation count anomalies from span cardinality alone.
   Available in every environment that sends APM traces.

If profiling is not enabled, the tools return profiling_available=False but
span-based pattern analysis still runs and can surface N+1 and hotspot patterns.
"""

import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ._runner import get_config

# Local profiling store (populated from OTLP fan-out)
try:
    sys.path.insert(0, "/app")
    from streaming import profiling_store as _profiling_store
except Exception:
    _profiling_store = None

logger = logging.getLogger(__name__)

_PROFILING_API = "/v2/apm/profiling"
_CALLGRAPH_API = "/v2/call-graphs"

# Lazily resolved org ID (required by the call-graph API as X-SF-OrgId).
_org_id_cache: str | None = None


def _api(path: str, payload: dict = None, method: str = "GET",
         extra_headers: dict | None = None) -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"API {method} {path} → {e.code}: {body}") from e


def _get_org_id() -> str | None:
    """Fetch and cache the Splunk org ID via /v2/organizations/member."""
    global _org_id_cache
    if _org_id_cache:
        return _org_id_cache
    try:
        data = _api("/v2/organizations/member")
        # Response is a list of org memberships; use the first org's id
        orgs = data if isinstance(data, list) else data.get("organizations", [data])
        for org in orgs:
            oid = org.get("id") or org.get("organizationId")
            if oid:
                _org_id_cache = str(oid)
                return _org_id_cache
    except Exception as exc:
        logger.debug("Could not fetch org ID: %s", exc)
    return None


def _signalflow(program: str, start_ms: int, end_ms: int) -> dict:
    """Execute a SignalFlow program, return {streams, metadata}."""
    cfg = get_config()
    qs = f"?start={start_ms}&stop={end_ms}&resolution=60000&immediate=true"
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute{qs}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "text/plain"}
    req = urllib.request.Request(url, data=program.encode(), headers=headers, method="POST")
    streams: dict = {}
    metadata: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            event_type = None
            data_lines: list = []
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("event: "):
                    event_type = line[7:].strip()
                    data_lines = []
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and data_lines:
                    try:
                        msg = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
                        continue
                    etype = event_type or msg.get("type", "")
                    if etype == "metadata":
                        tsid = msg.get("tsId", "")
                        if tsid:
                            metadata[tsid] = msg.get("properties", {})
                    elif etype == "data":
                        for pt in msg.get("data", []):
                            tsid = pt.get("tsId", "")
                            val = pt.get("value")
                            if tsid and val is not None:
                                streams.setdefault(tsid, []).append(float(val))
                    data_lines = []
    except Exception as exc:
        logger.warning("SignalFlow execute failed: %s", exc)
    return {"streams": streams, "metadata": metadata}


# ── Profiling API ──────────────────────────────────────────────────────────────

def get_profiling_services(environment: str) -> str:
    """
    List services that have AlwaysOn Profiling data available.

    Returns which services have CPU and/or memory profiling enabled, and
    the time range of available profiling data.

    Args:
        environment: Deployment environment name.
    """
    # Try local store first (populated from OTLP fan-out via OTel Collector)
    if _profiling_store is not None:
        local_services = _profiling_store.get_services(environment)
        if local_services:
            return json.dumps({
                "profiling_available": True,
                "source": "local_otlp_capture",
                "environment": environment,
                "service_count": len(local_services),
                "services": [{"name": s, "types": ["cpu"]} for s in local_services],
                "note": "Data from local OTLP fan-out capture (last 10 minutes).",
            }, indent=2)

    try:
        data = _api(f"{_PROFILING_API}/services?environment={environment}&limit=100")
        services = data.get("services", [])
        if not services:
            return json.dumps({
                "profiling_available": False,
                "environment": environment,
                "note": (
                    "No profiling data found for this environment. "
                    "AlwaysOn Profiling requires SPLUNK_PROFILER_ENABLED=true in the "
                    "Splunk OTel Java/Python agent. Span-based pattern analysis is still available."
                ),
            }, indent=2)
        return json.dumps({
            "profiling_available": True,
            "environment": environment,
            "service_count": len(services),
            "services": services,
        }, indent=2)
    except Exception as exc:
        # Profiling API may return 404 if the feature is not enabled
        return json.dumps({
            "profiling_available": False,
            "environment": environment,
            "error": str(exc),
            "note": (
                "AlwaysOn Profiling API unavailable. This may mean profiling is not enabled "
                "or the API endpoint differs for this realm. "
                "Span-based pattern analysis can still detect N+1 queries and hot operation paths."
            ),
        }, indent=2)


def get_cpu_flamegraph(service: str, environment: str, lookback_minutes: int = 60) -> str:
    """
    Get CPU flame graph for a service — shows which functions consume the most CPU time.

    Returns top stack frames ranked by CPU sample count with file paths and line numbers.
    Each frame represents a function that was on the call stack during CPU profiling samples.
    High sample counts = high CPU time = optimization target.

    Args:
        service: Service name.
        environment: Deployment environment name.
        lookback_minutes: How far back to look (default: 60).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000

    # Try local store first (populated from OTLP fan-out via OTel Collector)
    if _profiling_store is not None:
        local_frames = _profiling_store.get_flamegraph(service, environment)
        if local_frames:
            total_samples = sum(f["samples"] for f in local_frames)
            return json.dumps({
                "service": service,
                "environment": environment,
                "profiling_available": True,
                "source": "local_otlp_capture",
                "lookback_minutes": lookback_minutes,
                "total_cpu_samples": total_samples,
                "top_frames": local_frames,
                "interpretation": (
                    "Frames with high sample counts are consuming the most CPU time. "
                    "Look for application code frames (not framework/stdlib) with >5% CPU — "
                    "these are your optimization targets."
                ),
            }, indent=2)

    try:
        payload = {
            "service": service,
            "environment": environment,
            "profileType": "CPU",
            "startTime": start_ms,
            "endTime": now_ms,
            "limit": 50,
        }
        data = _api(f"{_PROFILING_API}/flamegraph", payload=payload, method="POST")

        frames = data.get("frames", data.get("nodes", []))
        if not frames:
            return json.dumps({
                "service": service,
                "profiling_available": False,
                "note": (
                    f"No CPU profiling data for {service} in the last {lookback_minutes}m. "
                    "Either profiling is not enabled for this service, or no CPU samples were collected."
                ),
            }, indent=2)

        # Normalize frame format
        normalized = []
        for f in frames[:30]:
            normalized.append({
                "function": f.get("function") or f.get("name") or f.get("symbol", ""),
                "file": f.get("file") or f.get("fileName") or f.get("filename", ""),
                "line": f.get("line") or f.get("lineNumber") or f.get("lineno", 0),
                "module": f.get("module") or f.get("class") or f.get("namespace", ""),
                "samples": int(f.get("samples") or f.get("value") or f.get("count", 0)),
                "pct_cpu": round(float(f.get("pctCpu") or f.get("percentage") or 0), 2),
            })
        normalized.sort(key=lambda x: -x["samples"])

        total_samples = sum(f["samples"] for f in normalized)
        return json.dumps({
            "service": service,
            "environment": environment,
            "profiling_available": True,
            "lookback_minutes": lookback_minutes,
            "total_cpu_samples": total_samples,
            "top_frames": normalized,
            "interpretation": (
                "Frames with high sample counts are consuming the most CPU time. "
                "Look for application code frames (not framework/stdlib) with >5% CPU — "
                "these are your optimization targets."
            ),
        }, indent=2)

    except Exception as exc:
        return json.dumps({
            "service": service,
            "profiling_available": False,
            "error": str(exc),
            "note": "CPU profiling data unavailable. Use analyze_span_call_patterns for span-based analysis.",
        }, indent=2)


def get_memory_profile(service: str, environment: str, lookback_minutes: int = 60) -> str:
    """
    Get memory allocation profile for a service — shows which functions allocate the most memory.

    High allocation counts in a single function can indicate memory leaks, excessive object
    creation, or unbounded caches. Correlate with heap growth over time to confirm leaks.

    Args:
        service: Service name.
        environment: Deployment environment name.
        lookback_minutes: How far back to look (default: 60).
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000

    try:
        payload = {
            "service": service,
            "environment": environment,
            "profileType": "MEMORY",
            "startTime": start_ms,
            "endTime": now_ms,
            "limit": 30,
        }
        data = _api(f"{_PROFILING_API}/flamegraph", payload=payload, method="POST")

        frames = data.get("frames", data.get("nodes", []))
        if not frames:
            return json.dumps({
                "service": service,
                "profiling_available": False,
                "note": f"No memory profiling data for {service}. Requires SPLUNK_PROFILER_MEMORY_ENABLED=true.",
            }, indent=2)

        normalized = []
        for f in frames[:20]:
            normalized.append({
                "function": f.get("function") or f.get("name", ""),
                "file": f.get("file") or f.get("fileName", ""),
                "line": f.get("line") or f.get("lineNumber", 0),
                "allocations": int(f.get("samples") or f.get("allocations") or f.get("value", 0)),
                "alloc_mb": round(float(f.get("allocMb") or f.get("bytes", 0)) / (1024 * 1024), 2),
            })
        normalized.sort(key=lambda x: -x["allocations"])

        return json.dumps({
            "service": service,
            "profiling_available": True,
            "lookback_minutes": lookback_minutes,
            "top_allocation_frames": normalized,
            "interpretation": (
                "Functions with high allocation counts are creating many objects. "
                "If allocations grow over time without corresponding GC reclamation, "
                "this indicates a memory leak. Check for unbounded collections or caches."
            ),
        }, indent=2)

    except Exception as exc:
        return json.dumps({
            "service": service,
            "profiling_available": False,
            "error": str(exc),
        }, indent=2)


def analyze_span_call_patterns(
    service: str,
    environment: str,
    lookback_minutes: int = 60,
) -> str:
    """
    Analyze APM span operation patterns for performance anti-patterns.

    Does NOT require AlwaysOn Profiling. Uses SignalFlow span metrics to detect:
    - N+1 query patterns: high DB operation count per service request
    - Hot operations: operations with very high call frequency
    - Synchronous fan-out: many parallel calls to the same operation
    - High-latency outliers: operations with P99 >> P50 (indicating occasional slow paths)

    This is always available regardless of profiling configuration.

    Args:
        service: Service name.
        environment: Deployment environment name.
        lookback_minutes: Lookback window.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000
    env_f = f"filter('sf_environment', '{environment}')"
    svc_f = f"filter('sf_service', '{service}')"

    # Operations by call count
    count_prog = (
        f"data('spans.count', filter={env_f} and {svc_f})"
        f".sum(by=['sf_operation']).publish(label='count')"
    )
    # P99 per operation
    p99_prog = (
        f"data('service.request.duration.ns.p99', filter={env_f} and {svc_f})"
        f".mean(by=['sf_operation']).publish(label='p99')"
    )
    # P50 per operation
    p50_prog = (
        f"data('service.request.duration.ns.p50', filter={env_f} and {svc_f})"
        f".mean(by=['sf_operation']).publish(label='p50')"
    )
    # Error count per operation
    err_prog = (
        f"data('spans.count', filter={env_f} and {svc_f} and filter('sf_error', 'true'))"
        f".sum(by=['sf_operation']).publish(label='errors')"
    )

    try:
        count_r = _signalflow(count_prog, start_ms, now_ms)
        p99_r = _signalflow(p99_prog, start_ms, now_ms)
        p50_r = _signalflow(p50_prog, start_ms, now_ms)
        err_r = _signalflow(err_prog, start_ms, now_ms)

        def _by_op(result: dict) -> dict[str, float]:
            out: dict[str, float] = {}
            for sid, vals in result["streams"].items():
                props = result["metadata"].get(sid, {})
                op = props.get("sf_operation") or props.get("span.name") or sid
                out[op] = out.get(op, 0) + sum(vals)
            return out

        op_count = _by_op(count_r)
        op_p99 = {op: v / 1e6 for op, v in _by_op(p99_r).items()}   # ns → ms
        op_p50 = {op: v / 1e6 for op, v in _by_op(p50_r).items()}
        op_errors = _by_op(err_r)

        # Find service-level request count to compute per-request ratios
        svc_reqs_prog = (
            f"data('service.request.count', filter={env_f} and {svc_f})"
            f".sum().publish(label='svc_reqs')"
        )
        svc_r = _signalflow(svc_reqs_prog, start_ms, now_ms)
        svc_reqs = sum(sum(v) for v in svc_r["streams"].values()) or 1

        # Build per-operation stats
        all_ops = sorted(set(op_count) | set(op_p99))
        operations = []
        for op in all_ops:
            count = round(op_count.get(op, 0))
            p99_ms = round(op_p99.get(op, 0), 1)
            p50_ms = round(op_p50.get(op, 0), 1)
            errors = round(op_errors.get(op, 0))
            ratio = round(count / svc_reqs, 1) if svc_reqs > 0 else None
            error_rate = round(errors / count * 100, 1) if count > 0 else 0
            latency_spread = round(p99_ms / p50_ms, 1) if p50_ms > 0 else None

            operations.append({
                "operation": op,
                "total_calls": count,
                "calls_per_request": ratio,
                "p99_ms": p99_ms,
                "p50_ms": p50_ms,
                "error_rate_pct": error_rate,
                "latency_spread_p99_p50": latency_spread,
            })

        operations.sort(key=lambda x: -(x["total_calls"] or 0))

        # Identify anti-patterns
        antipatterns = []
        db_ops = [o for o in operations if any(kw in o["operation"].lower()
                  for kw in ["select", "insert", "update", "delete", "query", "find",
                              "get", "set", "hget", "hset", "zadd", "db.", "sql"])]

        for op in db_ops:
            ratio = op.get("calls_per_request") or 0
            if ratio > 5:
                antipatterns.append({
                    "pattern": "n_plus_1_query",
                    "operation": op["operation"],
                    "calls_per_request": ratio,
                    "severity": "critical" if ratio > 20 else "high",
                    "description": (
                        f"Operation '{op['operation']}' called {ratio}× per service request "
                        f"— classic N+1 pattern. Each item in a collection triggers an "
                        f"individual DB query instead of a single batch query."
                    ),
                    "fix_pattern": (
                        "Replace per-item query with bulk SELECT WHERE id IN (...). "
                        "For ORMs: use prefetch_related() (Django), eager loading (SQLAlchemy), "
                        "or include() (ActiveRecord)."
                    ),
                })

        for op in operations:
            spread = op.get("latency_spread_p99_p50") or 0
            if spread > 10 and op["p99_ms"] > 500:
                antipatterns.append({
                    "pattern": "latency_outlier",
                    "operation": op["operation"],
                    "p99_ms": op["p99_ms"],
                    "p50_ms": op["p50_ms"],
                    "spread": spread,
                    "severity": "high",
                    "description": (
                        f"Operation '{op['operation']}' has P99={op['p99_ms']}ms vs "
                        f"P50={op['p50_ms']}ms (spread: {spread}×). "
                        f"This bimodal distribution indicates occasional very slow code paths — "
                        f"cold cache misses, lock contention, or GC pauses."
                    ),
                    "fix_pattern": (
                        "Add cache warming, investigate lock contention, "
                        "or tune GC settings. Use profiling to find the slow code path."
                    ),
                })

        high_error_ops = [o for o in operations if o["error_rate_pct"] > 5]

        return json.dumps({
            "service": service,
            "environment": environment,
            "lookback_minutes": lookback_minutes,
            "service_requests_total": round(svc_reqs),
            "operation_count": len(operations),
            "top_operations_by_call_count": operations[:20],
            "antipatterns_detected": antipatterns,
            "high_error_operations": high_error_ops[:10],
        }, indent=2)

    except Exception as exc:
        return f"[analyze_span_call_patterns error]: {exc}"


def get_thread_profile(service: str, environment: str, lookback_minutes: int = 30) -> str:
    """
    Get thread state distribution for a service — identifies blocking and contention.

    Thread states:
    - RUNNABLE: executing or ready to execute (healthy)
    - BLOCKED: waiting to acquire a monitor lock (lock contention)
    - WAITING / TIMED_WAITING: waiting on condition (I/O, sleep, join)

    High BLOCKED percentage = lock contention.
    High WAITING with no I/O = possible deadlock risk.

    Args:
        service: Service name.
        environment: Deployment environment name.
        lookback_minutes: Lookback window.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000

    try:
        payload = {
            "service": service,
            "environment": environment,
            "profileType": "THREADS",
            "startTime": start_ms,
            "endTime": now_ms,
        }
        data = _api(f"{_PROFILING_API}/threads", payload=payload, method="POST")

        states = data.get("threadStates", data.get("states", {}))
        if not states:
            return json.dumps({
                "service": service,
                "profiling_available": False,
                "note": "Thread profiling data unavailable.",
            }, indent=2)

        total = sum(states.values()) or 1
        state_pct = {k: round(v / total * 100, 1) for k, v in states.items()}

        issues = []
        blocked_pct = state_pct.get("BLOCKED", 0)
        if blocked_pct > 10:
            issues.append({
                "type": "lock_contention",
                "blocked_pct": blocked_pct,
                "description": f"{blocked_pct}% of threads are BLOCKED — lock contention detected.",
                "fix_pattern": "Reduce synchronized block scope, use ConcurrentHashMap/ReadWriteLock, or move to lock-free data structures.",
            })

        return json.dumps({
            "service": service,
            "profiling_available": True,
            "thread_state_distribution": state_pct,
            "thread_state_counts": states,
            "issues": issues,
        }, indent=2)

    except Exception as exc:
        return json.dumps({
            "service": service,
            "profiling_available": False,
            "error": str(exc),
        }, indent=2)


def get_slowest_methods(
    service: str,
    trace_id: str,
    from_epoch_ms: int,
    to_epoch_ms: int,
    limit: int = 5,
) -> str:
    """
    Get the slowest methods for a specific trace using the Splunk Call Graph API.

    Returns the top methods ranked by exclusive self-time (CPU time spent inside
    the method itself, excluding callees). This is trace-correlated profiling:
    you get the exact functions that were hot during THIS specific slow request,
    not a broad aggregate.

    Use this after finding a slow trace via analyze_span_call_patterns or
    search_error_traces to drill into which code was executing during that trace.

    Args:
        service:        Service name (must match the service in the trace).
        trace_id:       Trace ID from a slow APM span.
        from_epoch_ms:  Start of the time window (epoch milliseconds).
        to_epoch_ms:    End of the time window (epoch milliseconds).
        limit:          Max methods to return (default 5, max 5).
    """
    # Validate window: must be ≤ 24h
    window_ms = to_epoch_ms - from_epoch_ms
    if window_ms <= 0 or window_ms > 86_400_000:
        return json.dumps({
            "error": "Time window must be between 0 and 24 hours.",
            "from_epoch_ms": from_epoch_ms,
            "to_epoch_ms": to_epoch_ms,
        }, indent=2)

    limit = min(limit, 5)

    org_id = _get_org_id()
    if not org_id:
        return json.dumps({
            "profiling_available": False,
            "error": "Could not resolve Splunk org ID (X-SF-OrgId). "
                     "Set SPLUNK_ORG_ID env var or ensure the token has org-read scope.",
            "note": "Fall back to get_cpu_flamegraph for aggregate profiling data.",
        }, indent=2)

    try:
        path = (
            f"{_CALLGRAPH_API}/{urllib.request.quote(service, safe='')}"
            f"/{urllib.request.quote(trace_id, safe='')}"
            f"/slow-methods?from={from_epoch_ms}&to={to_epoch_ms}&limit={limit}"
        )
        data = _api(path, extra_headers={"X-SF-OrgId": org_id})

        methods = data.get("methods", [])
        metadata = data.get("metadata", {})
        reason = metadata.get("reason", "")

        if not methods or reason == "NO_SAMPLES":
            return json.dumps({
                "service": service,
                "trace_id": trace_id,
                "profiling_available": False,
                "reason": reason or "NO_SAMPLES",
                "metadata": metadata,
                "note": (
                    "No profiling samples found for this trace. "
                    "AlwaysOn Profiling must be enabled for this service and the trace "
                    "must fall within the profiling retention window. "
                    "Fall back to get_cpu_flamegraph for aggregate data."
                ),
            }, indent=2)

        # Normalize output — surface the fields the LLM needs for code-level analysis
        normalized = []
        for m in methods:
            normalized.append({
                "method": m.get("methodName", ""),
                "class": m.get("className", ""),
                "self_time_ms": round(float(m.get("totalSelfTimeMs", 0)), 2),
                "sample_count": m.get("sampleCount", 0),
                "exit_call": m.get("exitCall"),         # what the method was waiting on
                "exit_call_action": m.get("exitCallAction"),
            })

        total_self_ms = sum(m["self_time_ms"] for m in normalized)

        return json.dumps({
            "service": service,
            "trace_id": trace_id,
            "profiling_available": True,
            "source": "splunk_callgraph_api",
            "slowest_methods": normalized,
            "total_self_time_ms": round(total_self_ms, 2),
            "metadata": {
                "window_from_ms": metadata.get("windowFrom"),
                "window_to_ms": metadata.get("windowTo"),
                "total_candidates": metadata.get("totalCandidates"),
                "total_frames_scanned": metadata.get("totalFrames"),
            },
            "interpretation": (
                "self_time_ms is exclusive CPU time (time spent inside the method itself, "
                "not waiting on callees). High self_time = the method itself is the bottleneck. "
                "exit_call shows what the method was blocked on (I/O, locks, etc.) when it "
                "yielded — null means it was actively computing."
            ),
        }, indent=2)

    except RuntimeError as exc:
        err = str(exc)
        # Surface alpha/beta API issues with context so we can give feedback
        return json.dumps({
            "service": service,
            "trace_id": trace_id,
            "profiling_available": False,
            "error": err,
            "api_feedback": (
                "If this is a 404, the call-graph API may not be enabled for this org/realm. "
                "If 400, check that service and trace_id are non-empty and the time window "
                "is ≤ 24h. If 403, the token may be missing the required Splunk profiling scope."
            ),
            "note": "Fall back to get_cpu_flamegraph for aggregate profiling data.",
        }, indent=2)
    except Exception as exc:
        return json.dumps({
            "service": service,
            "trace_id": trace_id,
            "profiling_available": False,
            "error": str(exc),
        }, indent=2)


# ── Tool registry ──────────────────────────────────────────────────────────────

SCHEMAS = [
    {
        "toolSpec": {
            "name": "get_profiling_services",
            "description": (
                "List services that have AlwaysOn Profiling data available. "
                "Call this first to determine which services have CPU/memory profiling enabled. "
                "Returns profiling_available=false if profiling is not configured."
            ),
            "inputSchema": {"json": {"type": "object", "required": ["environment"],
                "properties": {"environment": {"type": "string"}}}},
        }
    },
    {
        "toolSpec": {
            "name": "get_cpu_flamegraph",
            "description": (
                "Get CPU flame graph for a service — top stack frames by CPU sample count. "
                "Returns file path, function name, line number, and sample count per frame. "
                "Requires AlwaysOn Profiling to be enabled. Use analyze_span_call_patterns "
                "as fallback if profiling is unavailable."
            ),
            "inputSchema": {"json": {"type": "object", "required": ["service", "environment"],
                "properties": {
                    "service": {"type": "string"},
                    "environment": {"type": "string"},
                    "lookback_minutes": {"type": "integer", "description": "Default: 60"},
                }}},
        }
    },
    {
        "toolSpec": {
            "name": "get_memory_profile",
            "description": (
                "Get memory allocation flame graph — functions that allocate the most objects. "
                "Use to identify memory leaks and excessive object creation. "
                "Requires SPLUNK_PROFILER_MEMORY_ENABLED=true."
            ),
            "inputSchema": {"json": {"type": "object", "required": ["service", "environment"],
                "properties": {
                    "service": {"type": "string"},
                    "environment": {"type": "string"},
                    "lookback_minutes": {"type": "integer"},
                }}},
        }
    },
    {
        "toolSpec": {
            "name": "analyze_span_call_patterns",
            "description": (
                "Analyze APM span operation patterns to detect performance anti-patterns. "
                "ALWAYS available — does not require profiling to be enabled. "
                "Detects: N+1 queries (high calls_per_request on DB operations), "
                "latency outliers (P99 >> P50), high-error operations. "
                "Call this for every service being analyzed, regardless of profiling availability."
            ),
            "inputSchema": {"json": {"type": "object", "required": ["service", "environment"],
                "properties": {
                    "service": {"type": "string"},
                    "environment": {"type": "string"},
                    "lookback_minutes": {"type": "integer"},
                }}},
        }
    },
    {
        "toolSpec": {
            "name": "get_slowest_methods",
            "description": (
                "Get the slowest methods for a specific trace using the Splunk Call Graph API. "
                "Returns top methods ranked by exclusive self-time (CPU time inside the method "
                "itself, not callees). This is trace-correlated: you get the exact hot functions "
                "for THIS slow request. "
                "Use after finding a slow trace in analyze_span_call_patterns or search_error_traces. "
                "Returns class name + method name — combine with get_source_context for Tier A analysis. "
                "Falls back gracefully if the API is unavailable (alpha/beta)."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "required": ["service", "trace_id", "from_epoch_ms", "to_epoch_ms"],
                "properties": {
                    "service":        {"type": "string", "description": "Service name from APM"},
                    "trace_id":       {"type": "string", "description": "Trace ID from a slow span"},
                    "from_epoch_ms":  {"type": "integer", "description": "Window start (epoch ms)"},
                    "to_epoch_ms":    {"type": "integer", "description": "Window end (epoch ms)"},
                    "limit":          {"type": "integer", "description": "Max methods (default 5, max 5)"},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_thread_profile",
            "description": (
                "Get thread state distribution — identifies lock contention and blocking I/O. "
                "High BLOCKED% = lock contention. High WAITING% = blocking I/O or sleep abuse. "
                "Requires AlwaysOn Profiling."
            ),
            "inputSchema": {"json": {"type": "object", "required": ["service", "environment"],
                "properties": {
                    "service": {"type": "string"},
                    "environment": {"type": "string"},
                    "lookback_minutes": {"type": "integer"},
                }}},
        }
    },
]

TOOL_FNS = {
    "get_profiling_services": get_profiling_services,
    "get_cpu_flamegraph": get_cpu_flamegraph,
    "get_memory_profile": get_memory_profile,
    "analyze_span_call_patterns": analyze_span_call_patterns,
    "get_slowest_methods": get_slowest_methods,
    "get_thread_profile": get_thread_profile,
}
