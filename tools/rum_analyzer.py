"""
RUM (Real User Monitoring) analysis tools for Splunk Observability Cloud.

Queries Splunk RUM metrics to assess frontend user experience:
- Session counts and engagement
- JavaScript error rates
- Core Web Vitals (LCP, FID/INP, CLS, TTI)
- Long tasks and resource load times
- RUM app configuration status

Uses SignalFlow to query rum.* metrics from the Splunk Observability API.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from ._runner import get_config

logger = logging.getLogger(__name__)

_RUM_APP_DIMENSION = "app"
_RUM_ENV_DIMENSION = "environment"


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


def _signalflow(program: str, hours: int = 1) -> dict:
    """Execute a SignalFlow program and return stream results."""
    cfg = get_config()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    payload = {
        "program": program,
        "start": start_ms,
        "stop": now_ms,
        "resolution": 60000,
        "immediate": True,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    results: dict[str, list] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            for line in resp:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "data":
                    for ts_str, payload_data in msg.get("data", {}).items():
                        for stream_id, val in payload_data.items():
                            if val is not None:
                                results.setdefault(stream_id, []).append(val)
    except Exception as exc:
        logger.warning("SignalFlow query failed: %s", exc)
    return results


def list_rum_apps() -> str:
    """
    List all RUM applications configured in the Splunk Observability org.

    Returns a JSON object with:
      - apps: list of {name, session_count_24h, error_count_24h}
      - configured: bool (whether any RUM app is reporting data)
      - message: human-readable summary
    """
    try:
        # Query the RUM metric catalog for known app dimension values
        result = _api("/v2/metrictimeseries?query=sf_metric:rum.page.views.count&limit=100", method="GET")
        apps: dict[str, dict] = {}
        for mts in result.get("results", []):
            dims = mts.get("dimensions", {})
            app = dims.get(_RUM_APP_DIMENSION, "")
            if app and app not in apps:
                apps[app] = {"name": app, "environment": dims.get(_RUM_ENV_DIMENSION, "")}

        if not apps:
            return json.dumps({
                "configured": False,
                "apps": [],
                "message": (
                    "No RUM applications found. RUM is not configured or no sessions have been "
                    "recorded in the last 7 days. To instrument the frontend, add the Splunk RUM "
                    "JavaScript snippet (see deploy/06-enable-rum.sh)."
                ),
            }, indent=2)

        return json.dumps({
            "configured": True,
            "apps": list(apps.values()),
            "message": f"Found {len(apps)} RUM application(s): {', '.join(apps.keys())}",
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": str(exc), "configured": False, "apps": []}, indent=2)


def get_rum_metrics(app_name: str, hours: int = 24) -> str:
    """
    Get RUM metrics for a specific application: session counts, page views,
    JavaScript error rate, and Core Web Vitals.

    Args:
      app_name: RUM application name (the `app` dimension)
      hours: lookback window (default 24)

    Returns JSON with session_count, error_count, error_rate, core_web_vitals,
    and a human-readable health summary.
    """
    try:
        filter_clause = f'filter("app", "{app_name}")'

        programs = {
            "sessions": f'data("rum.page.views.count", {filter_clause}).sum(over="{hours}h").publish()',
            "errors":   f'data("rum.js.errors.count", {filter_clause}).sum(over="{hours}h").publish()',
            "lcp_p75":  f'data("rum.lcp", {filter_clause}).percentile(pct=75).publish()',
            "fid_p75":  f'data("rum.fid", {filter_clause}).percentile(pct=75).publish()',
            "cls_p75":  f'data("rum.cls", {filter_clause}).percentile(pct=75).publish()',
            "tti_p75":  f'data("rum.time_to_interactive", {filter_clause}).percentile(pct=75).publish()',
        }

        metrics: dict[str, float | None] = {}
        for key, prog in programs.items():
            try:
                streams = _signalflow(prog, hours=hours)
                if streams:
                    vals = list(streams.values())
                    if vals and vals[0]:
                        metrics[key] = vals[0][-1]
                    else:
                        metrics[key] = None
                else:
                    metrics[key] = None
            except Exception:
                metrics[key] = None

        sessions = metrics.get("sessions") or 0
        errors = metrics.get("errors") or 0
        error_rate = (errors / sessions * 100) if sessions > 0 else 0.0

        lcp = metrics.get("lcp_p75")
        fid = metrics.get("fid_p75")
        cls_val = metrics.get("cls_p75")
        tti = metrics.get("tti_p75")

        # Core Web Vitals scoring (Google thresholds)
        def lcp_grade(v): return "good" if v and v < 2500 else ("needs-improvement" if v and v < 4000 else "poor")
        def fid_grade(v): return "good" if v and v < 100 else ("needs-improvement" if v and v < 300 else "poor")
        def cls_grade(v): return "good" if v and v < 0.1 else ("needs-improvement" if v and v < 0.25 else "poor")

        health = "healthy"
        issues = []
        if error_rate > 5:
            health = "degraded"
            issues.append(f"High JS error rate: {error_rate:.1f}%")
        if lcp and lcp > 4000:
            health = "degraded"
            issues.append(f"Poor LCP: {lcp:.0f}ms (threshold: <2500ms)")
        if cls_val and cls_val > 0.25:
            health = "degraded"
            issues.append(f"Poor CLS: {cls_val:.3f} (threshold: <0.1)")

        return json.dumps({
            "app": app_name,
            "window_hours": hours,
            "sessions": int(sessions),
            "js_errors": int(errors),
            "error_rate_pct": round(error_rate, 2),
            "core_web_vitals": {
                "lcp_p75_ms":  round(lcp, 0) if lcp else None,
                "lcp_grade":   lcp_grade(lcp),
                "fid_p75_ms":  round(fid, 0) if fid else None,
                "fid_grade":   fid_grade(fid),
                "cls_p75":     round(cls_val, 3) if cls_val else None,
                "cls_grade":   cls_grade(cls_val),
                "tti_p75_ms":  round(tti, 0) if tti else None,
            },
            "health": health,
            "issues": issues,
            "message": (
                f"{app_name}: {int(sessions)} sessions, {error_rate:.1f}% error rate "
                f"in last {hours}h. LCP={lcp_grade(lcp)}, FID={fid_grade(fid)}, CLS={cls_grade(cls_val)}."
            ),
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": str(exc), "app": app_name}, indent=2)


def get_rum_errors(app_name: str, hours: int = 6) -> str:
    """
    Get top JavaScript error types for a RUM application.

    Args:
      app_name: RUM application name
      hours: lookback window (default 6)

    Returns JSON with top_errors list and total count.
    """
    try:
        filter_clause = f'filter("app", "{app_name}")'
        prog = (
            f'data("rum.js.errors.count", {filter_clause})'
            f'.sum(by=["error.type", "error.message"]).top(count=10).publish()'
        )
        streams = _signalflow(prog, hours=hours)

        errors = []
        for stream_id, vals in streams.items():
            if vals:
                errors.append({"stream_id": stream_id, "count": sum(v for v in vals if v)})

        errors.sort(key=lambda x: x["count"], reverse=True)
        total = sum(e["count"] for e in errors)

        return json.dumps({
            "app": app_name,
            "window_hours": hours,
            "total_errors": int(total),
            "top_errors": errors[:10],
            "message": f"{app_name}: {int(total)} JS errors in last {hours}h. Top {len(errors[:10])} error types shown.",
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": str(exc), "app": app_name}, indent=2)


SCHEMAS = [
    {
        "toolSpec": {
            "name": "list_rum_apps",
            "description": "List all RUM applications reporting data to Splunk Observability Cloud. Call this first to discover what apps are instrumented.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "get_rum_metrics",
            "description": "Get RUM health metrics for a specific app: session counts, JS error rate, Core Web Vitals (LCP, FID, CLS, TTI).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["app_name"],
                    "properties": {
                        "app_name": {"type": "string", "description": "RUM application name (the `app` dimension)"},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default 24)"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_rum_errors",
            "description": "Get top JavaScript error types for a RUM app, with counts per error type.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["app_name"],
                    "properties": {
                        "app_name": {"type": "string"},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default 6)"},
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "list_rum_apps": list_rum_apps,
    "get_rum_metrics": get_rum_metrics,
    "get_rum_errors": get_rum_errors,
}
