"""
Dashboard provisioning tool — creates the O11y Agent self-observability dashboard
in Splunk Observability Cloud via the REST API.

The dashboard shows agent health: run duration, issues by severity,
instrumentation score trend, silent service count, and streaming alert volume.
"""

import json
import logging
import urllib.error
import urllib.request

from ._runner import get_config

logger = logging.getLogger(__name__)


def _api(path: str, payload: dict, method: str = "POST") -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API {method} {path} → {e.code}: {body}") from e


def provision_agent_dashboard(environment: str = "") -> str:
    """
    Create (or locate existing) the O11y Agent self-observability dashboard.

    Args:
      environment: deployment environment to scope the dashboard (optional)

    Returns JSON with dashboard_id, dashboard_url, and status.
    """
    cfg = get_config()
    env_filter = environment or cfg.environment
    env_label = f" — {env_filter}" if env_filter else ""

    charts = _build_charts(env_filter)

    dashboard_payload = {
        "name": f"O11y Agent Self-Observability{env_label}",
        "description": (
            "Autonomous O11y Agent health: assessment runs, issues found by severity, "
            "instrumentation score, silent service tracking, and streaming alert volume."
        ),
        "charts": charts,
        "filters": {
            "variables": [
                {
                    "property": "environment",
                    "alias": "environment",
                    "description": "Deployment environment",
                    "value": [env_filter] if env_filter else [],
                    "required": False,
                    "replaceOnly": False,
                }
            ]
        },
    }

    try:
        result = _api("/v2/dashboard", dashboard_payload)
        dashboard_id = result.get("id", "")
        realm = cfg.realm
        url = f"https://app.{realm}.signalfx.com/#/dashboard/{dashboard_id}"
        logger.info("Dashboard created: %s", url)
        return json.dumps({
            "status": "created",
            "dashboard_id": dashboard_id,
            "dashboard_url": url,
            "message": f"O11y Agent dashboard created: {url}",
        }, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)}, indent=2)


def _build_charts(env_filter: str) -> list[dict]:
    """Build the chart definitions for the agent dashboard."""
    f = f'filter("environment", "{env_filter}")' if env_filter else ""
    fand = f" and {f}" if f else ""

    def sf(program): return {"programOptions": {"minimumResolution": 0}, "programText": program}

    charts = [
        # 1. Assessment run duration (last 24h)
        {
            "name": "Assessment Run Duration (seconds)",
            "options": {"type": "TimeSeriesChart", "defaultPlotType": "AreaChart"},
            **sf(
                f'A = data("o11y_agent.run.duration"{", " + f if f else ""})'
                '.mean(over="5m").publish(label="Run Duration")'
            ),
        },
        # 2. Issues found by severity
        {
            "name": "Issues Found by Severity",
            "options": {"type": "TimeSeriesChart", "defaultPlotType": "LineChart"},
            **sf(
                f'data("o11y_agent.issues.found"{", " + f if f else ""})'
                '.sum(by=["severity"]).publish(label="Issues by Severity")'
            ),
        },
        # 3. Instrumentation score
        {
            "name": "Instrumentation Score (0-100)",
            "options": {"type": "TimeSeriesChart", "defaultPlotType": "LineChart"},
            **sf(
                f'data("o11y_agent.instrumentation_score"{", " + f if f else ""})'
                '.mean().publish(label="Score")'
            ),
        },
        # 4. Silent services
        {
            "name": "Silent Services Count",
            "options": {"type": "TimeSeriesChart", "defaultPlotType": "AreaChart"},
            **sf(
                f'data("o11y_agent.silent_services"{", " + f if f else ""})'
                '.mean().publish(label="Silent Services")'
            ),
        },
        # 5. Single-value: current score
        {
            "name": "Current Instrumentation Score",
            "options": {"type": "SingleValueChart", "colorBy": "Scale",
                        "colorScale2": [
                            {"gt": 80, "color": "green"},
                            {"gt": 50, "color": "yellow"},
                            {"color": "red"},
                        ]},
            **sf(
                f'data("o11y_agent.instrumentation_score"{", " + f if f else ""})'
                '.mean().publish()'
            ),
        },
        # 6. Single-value: current silent services
        {
            "name": "Silent Services (current)",
            "options": {"type": "SingleValueChart"},
            **sf(
                f'data("o11y_agent.silent_services"{", " + f if f else ""})'
                '.mean().publish()'
            ),
        },
    ]
    return charts


SCHEMAS = [
    {
        "toolSpec": {
            "name": "provision_agent_dashboard",
            "description": (
                "Create the O11y Agent self-observability dashboard in Splunk Observability Cloud. "
                "Shows run duration, issues by severity, instrumentation score, and silent services."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "environment": {
                            "type": "string",
                            "description": "Environment to scope dashboard filters (optional, defaults to current environment)",
                        }
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "provision_agent_dashboard": provision_agent_dashboard,
}
