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
_RUM_ENV_DIMENSION = "sf_environment"


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
    # start/stop/resolution/immediate are query params, NOT JSON body fields —
    # the execute endpoint's body is the raw program text (text/plain).
    import urllib.parse
    qs = urllib.parse.urlencode({
        "start": start_ms,
        "stop": now_ms,
        "resolution": 60000,
        "immediate": "true",
    })
    url = f"https://stream.{cfg.realm}.signalfx.com/v2/signalflow/execute?{qs}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "text/plain"}
    req = urllib.request.Request(
        url, data=program.encode(), headers=headers, method="POST"
    )
    series: dict[str, list] = {}
    meta: dict[str, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    except Exception as exc:
        logger.warning("SignalFlow query failed: %s", exc)
    return {"series": series, "meta": meta}


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
        result = _api("/v2/metrictimeseries?query=sf_metric:rum.page_view.count&limit=100", method="GET")
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

        # rum.webvitals_*.p75 metrics are pre-aggregated per-timeseries (split by
        # browser/OS/etc.) — .mean() collapses those dimensions to one overall figure.
        # There is no Splunk RUM "time to interactive" metric; INP has replaced FID
        # in current Core Web Vitals, so there's no rum.fid equivalent either.
        programs = {
            "sessions": f'data("rum.page_view.count", {filter_clause}).sum(over="{hours}h").sum().publish()',
            "errors":   f'data("rum.client_error.count", {filter_clause}).sum(over="{hours}h").sum().publish()',
            "lcp_p75":  f'data("rum.webvitals_lcp.time.ns.p75", {filter_clause}).mean().publish()',
            "inp_p75":  f'data("rum.webvitals_inp.time.ns.p75", {filter_clause}).mean().publish()',
            "cls_p75":  f'data("rum.webvitals_cls.score.p75", {filter_clause}).mean().publish()',
        }

        metrics: dict[str, float | None] = {}
        for key, prog in programs.items():
            try:
                series = _signalflow(prog, hours=hours).get("series", {})
                vals = list(series.values())
                metrics[key] = vals[0][-1] if vals and vals[0] else None
            except Exception:
                metrics[key] = None

        sessions = metrics.get("sessions") or 0
        errors = metrics.get("errors") or 0
        error_rate = (errors / sessions * 100) if sessions > 0 else 0.0

        # rum.webvitals_lcp/inp.time.ns.p75 are reported in nanoseconds — convert to ms.
        lcp = metrics.get("lcp_p75") / 1e6 if metrics.get("lcp_p75") is not None else None
        inp = metrics.get("inp_p75") / 1e6 if metrics.get("inp_p75") is not None else None
        cls_val = metrics.get("cls_p75")

        # Core Web Vitals scoring (Google thresholds; INP replaces FID)
        def lcp_grade(v): return "good" if v and v < 2500 else ("needs-improvement" if v and v < 4000 else "poor")
        def inp_grade(v): return "good" if v and v < 200 else ("needs-improvement" if v and v < 500 else "poor")
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
                "inp_p75_ms":  round(inp, 0) if inp else None,
                "inp_grade":   inp_grade(inp),
                "cls_p75":     round(cls_val, 3) if cls_val else None,
                "cls_grade":   cls_grade(cls_val),
            },
            "health": health,
            "issues": issues,
            "message": (
                f"{app_name}: {int(sessions)} sessions, {error_rate:.1f}% error rate "
                f"in last {hours}h. LCP={lcp_grade(lcp)}, INP={inp_grade(inp)}, CLS={cls_grade(cls_val)}."
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
            f'data("rum.client_error.count", {filter_clause})'
            f'.sum(by=["errorId", "sf_operation"]).top(count=10).publish()'
        )
        result = _signalflow(prog, hours=hours)
        series = result.get("series", {})
        meta = result.get("meta", {})

        errors = []
        for tsid, vals in series.items():
            if vals:
                props = meta.get(tsid, {})
                label = f"{props.get('sf_operation', 'unknown')} (errorId={props.get('errorId', tsid)})"
                errors.append({"error": label, "count": sum(v for v in vals if v)})

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
