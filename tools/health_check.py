"""
Health check tool — wraps splunk-o11y-health-check scripts.

Runs focused checks (APM, detectors, OTel collectors) using the individual
domain scripts rather than the full orchestrator, which keeps runtime manageable
for the agent loop.
"""

import json
import tempfile
from pathlib import Path
from ._runner import get_config, run, summarise


def _run_script(script: str, extra_args: list = None) -> tuple[int, str, str]:
    cfg = get_config()
    cmd = [
        f"scripts/{script}",
        "--realm", cfg.realm,
    ]
    if extra_args:
        cmd.extend(extra_args)
    return run(cmd, cwd=cfg.health_check_path)


def _filter_detector_checks(data: dict, environment: str) -> dict:
    """Keep only detectors whose name contains the environment string (case-insensitive)."""
    env_lower = environment.lower()
    checks = data.get("checks", {})
    for key, check in checks.items():
        if isinstance(check, dict) and "rows" in check:
            check["rows"] = [
                r for r in check["rows"]
                if env_lower in str(r.get("detectorName", "")).lower()
            ]
    # Update summary counts to reflect filtered set
    data["_environmentFilter"] = environment
    return data


def check_detector_health(max_detectors: int = 200) -> str:
    """
    Audit the health of deployed detectors scoped to the configured environment.

    Identifies:
    - Detectors alerting on inactive/missing MTS (ghost detectors — firing on nothing)
    - Detectors that have never fired (potentially redundant coverage)
    - Detectors firing too frequently (alert fatigue risk)
    - Active muting rules and their scope
    - Overall detector inventory and coverage summary

    Results are filtered to detectors belonging to the configured environment only.

    Args:
        max_detectors: Maximum number of detectors to analyze (default: 200).
    """
    cfg = get_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name

    rc, stdout, stderr = _run_script(
        "o11y_detectors_health_check.py",
        ["--structured-json-out", out_path, "--max-detectors", str(max_detectors)],
    )

    try:
        with open(out_path) as f:
            data = json.load(f)
        Path(out_path).unlink(missing_ok=True)
        filtered = _filter_detector_checks(data, cfg.environment)
        return json.dumps(filtered, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_detector_health")


def _filter_apm_checks(data: dict, environment: str) -> dict:
    """Keep only service rows that belong to the configured environment."""
    checks = data.get("checks", {})
    for key, check in checks.items():
        if isinstance(check, dict) and "rows" in check:
            filtered_rows = []
            for r in check["rows"]:
                env_val = str(r.get("environment") or r.get("sf_environment") or "").strip()
                # Include if environment matches or row has no environment field (not environment-scoped)
                if not env_val or env_val == "—" or env_val.lower() == environment.lower():
                    filtered_rows.append(r)
            check["rows"] = filtered_rows
    data["_environmentFilter"] = environment
    return data


def check_apm_health(hours: int = 24) -> str:
    """
    Check the health of APM services scoped to the configured environment.

    Assesses:
    - Service coverage (how many services are instrumented and reporting)
    - Error rate patterns across services and endpoints
    - Latency distribution and p99 trends
    - Trace ingestion health and sampling rates
    - Services that recently stopped reporting (silent service detection)

    Results are filtered to the configured environment only.

    Args:
        hours: Lookback window in hours (default: 24).
    """
    cfg = get_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name

    rc, stdout, stderr = _run_script(
        "o11y_apm_health_check.py",
        ["--json-out", out_path, "--hours", str(hours)],
    )

    try:
        with open(out_path) as f:
            data = json.load(f)
        Path(out_path).unlink(missing_ok=True)
        filtered = _filter_apm_checks(data, cfg.environment)
        return json.dumps(filtered, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_apm_health")


def check_otel_collector_health(lookback_hours: int = 4) -> str:
    """
    Check the health and version status of all OpenTelemetry Collectors in the org.

    Identifies:
    - Collector versions (current vs deprecated)
    - Collectors that have stopped reporting
    - Exporter error rates and pipeline health
    - Recommended upgrade paths for deprecated versions

    Use this to ensure the telemetry pipeline infrastructure is healthy before
    trusting cardinality scans or instrumentation analysis.

    Args:
        lookback_hours: Hours of collector metrics to analyze (default: 4).
    """
    cfg = get_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name

    rc, stdout, stderr = _run_script(
        "o11y_otel_collectors_health_check.py",
        ["--structured-json-out", out_path, "--lookback-hours", str(lookback_hours)],
    )

    try:
        with open(out_path) as f:
            data = json.load(f)
        Path(out_path).unlink(missing_ok=True)
        return json.dumps(data, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_otel_collector_health")


def check_license_utilization(days: int = 30) -> str:
    """
    Check current license utilization against entitlements for Splunk Observability Cloud.

    Returns utilization percentages for:
    - APM hosts and trace ingest volume
    - Infrastructure Monitoring hosts and MTS
    - RUM sessions and MMS
    - Synthetics test runs

    Use this to understand capacity headroom and flag accounts approaching limits.

    Args:
        days: Lookback window in days (default: 30).
    """
    cfg = get_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name

    rc, stdout, stderr = _run_script(
        "o11y_license_utilization.py",
        ["--json-out", out_path, "--days", str(days)],
    )

    try:
        with open(out_path) as f:
            data = json.load(f)
        Path(out_path).unlink(missing_ok=True)
        return json.dumps(data, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_license_utilization")


SCHEMAS = [
    {
        "toolSpec": {
            "name": "check_detector_health",
            "description": (
                "Audit the health of deployed detectors scoped to the configured environment. "
                "Identifies ghost detectors (firing on dead MTS), noisy detectors, never-fired "
                "detectors, muted detectors, and inactive alert destinations."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "max_detectors": {
                            "type": "integer",
                            "description": "Maximum number of detectors to analyze (default: 200)",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "check_apm_health",
            "description": (
                "Check APM service health scoped to the configured environment. Returns service "
                "list with trace volumes, silent services, health check span pollution, sensitive "
                "data exposure, orphan services, and high-cardinality APM tags."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Lookback window in hours (default: 24)",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "check_otel_collector_health",
            "description": (
                "Check health and version status of all OTel Collectors in the org. "
                "Identifies deprecated versions, stopped collectors, and exporter error rates."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "lookback_hours": {
                            "type": "integer",
                            "description": "Hours of collector metrics to analyze (default: 4)",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "check_license_utilization",
            "description": (
                "Check license utilization against entitlements for APM hosts, MTS, "
                "RUM sessions, and Synthetics test runs."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Lookback window in days (default: 30)",
                        }
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "check_detector_health": check_detector_health,
    "check_apm_health": check_apm_health,
    "check_otel_collector_health": check_otel_collector_health,
    "check_license_utilization": check_license_utilization,
}
