"""
Log analysis tool — queries Splunk Observability Cloud for log signals.

Uses the Splunk Observability REST API directly (no subprocess dependency).
Requires Log Observer or Log Observer Connect to be configured in the org.

Endpoints used:
  POST https://api.{realm}.signalfx.com/v1/timeserieswindow  — log metric queries
  POST https://api.{realm}.signalfx.com/v2/signalflow/execute — SignalFlow queries
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from ._runner import get_config

logger = logging.getLogger(__name__)

_LOG_METRIC_PREFIX = "sf.org.num"   # Splunk internal log volume metrics


def _api(path: str, payload: dict = None, method: str = "POST") -> dict:
    """Make an authenticated call to the Splunk Observability REST API."""
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    headers = {
        "X-SF-TOKEN": cfg.token,
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API {method} {path} returned {e.code}: {body}") from e


def _signalflow(program: str, start_ms: int, end_ms: int, resolution_ms: int = 60000) -> dict:
    """
    Execute a SignalFlow program.

    Returns {"series": {tsId: [values]}, "meta": {tsId: {property: value}}} —
    metadata messages carry the dimensions/properties needed to map a tsId
    back to a human-readable series name (e.g. service.name).
    """
    import urllib.parse
    cfg = get_config()
    # start/stop/resolution/immediate are query params, NOT JSON body fields —
    # the execute endpoint's body is the raw program text (text/plain).
    qs = urllib.parse.urlencode({
        "start": start_ms,
        "stop": end_ms,
        "resolution": resolution_ms,
        "immediate": "true",
    })
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute?{qs}"
    headers = {
        "X-SF-TOKEN": cfg.token,
        "Content-Type": "text/plain",
    }
    req = urllib.request.Request(url, data=program.encode(), headers=headers, method="POST")
    series: dict[str, list] = {}
    meta: dict[str, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # SignalFlow streams Server-Sent Events: "event: <type>" then one or
            # more "data: <json-fragment>" lines, terminated by a blank line.
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
                    if etype == "data":
                        for point in msg.get("data", []):
                            tsid = point.get("tsId", "")
                            val = point.get("value")
                            if tsid and val is not None:
                                series.setdefault(tsid, []).append(float(val))
                    elif etype == "metadata":
                        tsid = msg.get("tsId", "")
                        if tsid:
                            meta[tsid] = msg.get("properties", {})
                    data_lines = []
        return {"series": series, "meta": meta}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"SignalFlow execute returned {e.code}: {body}") from e


def search_error_logs(service: str = "", hours: int = 1, limit: int = 50) -> str:
    """
    Search for ERROR and CRITICAL level log lines for a service (or all services).

    Uses Splunk Observability Log Observer API. Returns a summary of error
    patterns, counts, and representative log messages to help identify
    application-level failures that may not surface in APM error rates alone.

    Args:
        service: Service name to scope to. Leave empty for all services.
        hours: Number of hours to look back (default: 1).
        limit: Maximum number of log entries to return (default: 50).
    """
    cfg = get_config()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # Build filter
    filters = [
        {"field": "severity", "in": ["ERROR", "CRITICAL", "FATAL", "error", "critical", "fatal"]},
        {"field": "deployment.environment", "eq": cfg.environment},
    ]
    if service:
        filters.append({"field": "service.name", "eq": service})

    try:
        payload = {
            "filter": {"and": filters},
            "from": start_ms,
            "to": end_ms,
            "limit": limit,
            "orderBy": "-timestamp",
        }
        result = _api("/v1/log/entries", payload)
        entries = result.get("entries", [])

        if not entries:
            scope = f"service={service}" if service else "all services"
            return f"No ERROR/CRITICAL log entries found for {scope} in the last {hours}h."

        # Group by service and summarize patterns
        by_service: dict[str, list[str]] = {}
        for entry in entries:
            svc = entry.get("fields", {}).get("service.name", "unknown")
            msg = entry.get("message", entry.get("body", {}).get("stringValue", ""))[:200]
            by_service.setdefault(svc, []).append(msg)

        lines = [f"## Error Log Summary — last {hours}h\n"]
        lines.append(f"Total entries: {len(entries)} (limit={limit})\n")
        for svc, msgs in sorted(by_service.items()):
            lines.append(f"### {svc} ({len(msgs)} errors)")
            for msg in msgs[:5]:
                lines.append(f"  - {msg}")
            if len(msgs) > 5:
                lines.append(f"  ... and {len(msgs) - 5} more")
        return "\n".join(lines)

    except RuntimeError as e:
        err = str(e)
        if "404" in err or "403" in err:
            return (
                f"Log Observer API not available (error: {err}). "
                "Log analysis requires Log Observer or Log Observer Connect "
                "to be enabled in this Splunk Observability org."
            )
        return f"Error querying logs: {e}"


def analyze_log_patterns(service: str = "", hours: int = 4) -> str:
    """
    Identify recurring error patterns and anomalies in log data.

    Groups log messages by fingerprint to surface the top recurring error
    patterns rather than listing individual entries. Useful for finding
    noisy error sources that inflate error counts without being actionable.

    Args:
        service: Service name to scope to. Leave empty for all services.
        hours: Number of hours to look back (default: 4).
    """
    cfg = get_config()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    filters = [
        {"field": "severity", "in": ["ERROR", "CRITICAL", "FATAL", "error", "critical", "fatal", "WARN", "warn", "WARNING"]},
        {"field": "deployment.environment", "eq": cfg.environment},
    ]
    if service:
        filters.append({"field": "service.name", "eq": service})

    try:
        payload = {
            "filter": {"and": filters},
            "from": start_ms,
            "to": end_ms,
            "limit": 200,
            "orderBy": "-timestamp",
        }
        result = _api("/v1/log/entries", payload)
        entries = result.get("entries", [])

        if not entries:
            return f"No log entries found for analysis in the last {hours}h."

        # Simple pattern grouping: strip numbers/UUIDs/IPs from messages
        import re
        _token_re = re.compile(
            r'\b(?:[0-9a-f]{8}-[0-9a-f-]{27}|'  # UUID
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|'  # IP
            r'\d+)\b',
            re.IGNORECASE,
        )

        pattern_counts: dict[str, tuple[int, str]] = {}
        for entry in entries:
            raw_msg = entry.get("message", entry.get("body", {}).get("stringValue", ""))[:300]
            pattern = _token_re.sub("<X>", raw_msg)[:150]
            count, example = pattern_counts.get(pattern, (0, raw_msg))
            pattern_counts[pattern] = (count + 1, example)

        # Top 15 patterns by count
        top = sorted(pattern_counts.items(), key=lambda x: x[1][0], reverse=True)[:15]

        lines = [f"## Log Pattern Analysis — last {hours}h\n"]
        lines.append(f"Total entries analyzed: {len(entries)}")
        lines.append(f"Unique patterns: {len(pattern_counts)}\n")
        lines.append("**Top recurring patterns:**")
        for pattern, (count, example) in top:
            lines.append(f"  [{count}x] {example[:120]}")

        return "\n".join(lines)

    except RuntimeError as e:
        err = str(e)
        if "404" in err or "403" in err:
            return (
                "Log Observer API not available. "
                "Log pattern analysis requires Log Observer or Log Observer Connect."
            )
        return f"Error analyzing log patterns: {e}"


def get_log_volume(service: str = "", hours: int = 24) -> str:
    """
    Get log ingestion volume per service over a time window.

    Uses SignalFlow to query log line counts. Surfaces services generating
    unexpectedly high log volumes (cost drivers) and services with zero
    log output (instrumentation gaps).

    Args:
        service: Service name to scope to. Leave empty for all services.
        hours: Number of hours to look back (default: 24).
    """
    cfg = get_config()
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    filter_clause = f'filter("deployment.environment", "{cfg.environment}")'
    if service:
        filter_clause += f'.and(filter("service.name", "{service}"))'

    # SignalFlow: count log lines grouped by service
    program = (
        f"data('sf.org.numLogLines', {filter_clause})"
        f".sum(by=['service.name']).publish()"
    )

    try:
        result = _signalflow(program, start_ms, end_ms)
        raw_series = result.get("series", {})
        raw_meta = result.get("meta", {})

        # Sum each tsId's data points into a single total count
        series: dict[str, float] = {tsid: sum(vals) for tsid, vals in raw_series.items()}

        # Map tsid → service name via metadata properties
        meta: dict[str, str] = {
            tsid: props.get("service.name", tsid) for tsid, props in raw_meta.items()
        }

        if not series:
            return f"No log volume data found for environment={cfg.environment} in the last {hours}h."

        named = {meta.get(tsid, tsid): count for tsid, count in series.items()}
        top = sorted(named.items(), key=lambda x: x[1], reverse=True)

        lines = [f"## Log Volume — last {hours}h (environment={cfg.environment})\n"]
        lines.append(f"Total services with log data: {len(top)}")
        lines.append(f"Total log lines: {sum(named.values()):,}\n")
        lines.append("**Per-service volume (highest first):**")
        for svc, count in top[:20]:
            lines.append(f"  {svc}: {count:,.0f} lines")
        if len(top) > 20:
            lines.append(f"  ... and {len(top) - 20} more services")

        # Flag suspicious patterns
        total = sum(named.values())
        if total > 0:
            dominant = [(s, c) for s, c in top if c / total > 0.5]
            if dominant:
                svc, c = dominant[0]
                lines.append(
                    f"\n**WARNING:** `{svc}` accounts for {c/total:.0%} of all log volume "
                    "— potential log spam or excessive verbosity."
                )

        return "\n".join(lines)

    except RuntimeError as e:
        err = str(e)
        if "404" in err or "403" in err:
            return (
                "Log volume SignalFlow query failed. "
                "Ensure log ingestion is configured and the API token has log read permissions."
            )
        return f"Error querying log volume: {e}"


SCHEMAS = [
    {
        "toolSpec": {
            "name": "search_error_logs",
            "description": (
                "Search for ERROR and CRITICAL level log entries for a service. "
                "Returns error counts, representative messages, and service breakdown. "
                "Use this to find application errors that don't surface in APM error rates."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name to scope to. Leave empty for all services.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Hours to look back (default: 1).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max log entries to return (default: 50).",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "analyze_log_patterns",
            "description": (
                "Identify top recurring error patterns in logs by fingerprinting messages. "
                "Groups similar messages to surface the most common error sources. "
                "Useful for finding noisy errors and distinguishing signal from noise."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name to scope to. Leave empty for all services.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Hours to look back (default: 4).",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_log_volume",
            "description": (
                "Get log ingestion volume per service. Surfaces services generating "
                "unexpectedly high log volumes (cost drivers) and services with zero "
                "log output (instrumentation gaps)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name to scope to. Leave empty for all services.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Hours to look back (default: 24).",
                        },
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "search_error_logs": search_error_logs,
    "analyze_log_patterns": analyze_log_patterns,
    "get_log_volume": get_log_volume,
}
