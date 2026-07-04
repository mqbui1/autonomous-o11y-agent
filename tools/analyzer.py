"""
Instrumentation analyzer tool — wraps o11y-instrumentation-analyzer/analyze.py.
"""

import json
import tempfile
from pathlib import Path
from ._runner import get_config, run, summarise


def analyze_instrumentation(
    service: str = "",
    lookback_hours: int = 1,
    skip_logs: bool = False,
) -> str:
    """
    Analyze APM spans, infrastructure metrics, and logs for missing attributes and
    dimensions that break Related Content, Service Centric view, and trace-log correlation
    in Splunk Observability Cloud.

    Returns a 0–100 health score per signal with specific gaps identified:
    - APM: missing service.name, deployment.environment, host.name, k8s resource attrs
    - Metrics: missing sf_environment, host dimensions, Kubernetes labels
    - Logs: missing trace_id/span_id (breaks APM↔Logs), service.name, host.name
    - Cross-signal: which Related Content links are functional vs broken

    Score interpretation:
    90–100: Excellent — all key attributes present
    70–89:  Good — minor gaps, Related Content mostly functional
    50–69:  Fair — critical attributes missing, expect partial RC failures
    0–49:   Poor — significant gaps, Related Content broken

    Args:
        service: Scope analysis to a specific service. Leave empty for all services.
        lookback_hours: How many hours of recent telemetry to sample (default: 1).
        skip_logs: If True, skip log analysis (faster, use when logs not configured).
    """
    cfg = get_config()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name

    cmd = [
        "analyze.py",
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--format", "json",
        "--output", out_path,
        "--lookback-hours", str(lookback_hours),
    ]
    if cfg.environment:
        cmd.extend(["--environment", cfg.environment])
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])
    if skip_logs:
        cmd.append("--skip-logs")

    rc, stdout, stderr = run(cmd, cwd=cfg.analyzer_path)

    # Try to parse the JSON output for a clean summary
    try:
        with open(out_path) as f:
            data = json.load(f)
        Path(out_path).unlink(missing_ok=True)
        return json.dumps(data, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "analyze_instrumentation")


SCHEMAS = [
    {
        "toolSpec": {
            "name": "analyze_instrumentation",
            "description": (
                "Analyze APM spans, infrastructure metrics, and logs for missing attributes "
                "that break Related Content, Service Centric View, and trace-log correlation. "
                "Returns a 0–100 health score per signal type with specific gaps identified."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Scope to a specific service. Leave empty for all.",
                        },
                        "lookback_hours": {
                            "type": "integer",
                            "description": "Hours of recent telemetry to sample (default: 1)",
                        },
                        "skip_logs": {
                            "type": "boolean",
                            "description": "If true, skip log analysis (faster when logs not configured)",
                        },
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "analyze_instrumentation": analyze_instrumentation,
}
