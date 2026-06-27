"""
Health check tool — wraps splunk-o11y-health-check scripts.

Runs focused checks (APM, detectors, OTel collectors) using the individual
domain scripts rather than the full orchestrator, which keeps runtime manageable
for the agent loop.
"""

import json
import tempfile
from pathlib import Path
from strands import tool
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


@tool
def check_detector_health(max_detectors: int = 200) -> str:
    """
    Audit the health of all deployed detectors in the Splunk org.

    Identifies:
    - Detectors alerting on inactive/missing MTS (ghost detectors — firing on nothing)
    - Detectors that have never fired (potentially redundant coverage)
    - Detectors firing too frequently (alert fatigue risk)
    - Active muting rules and their scope
    - Overall detector inventory and coverage summary

    Use this to understand the quality of existing detector coverage before
    provisioning new detectors or retuning existing ones.

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
        return json.dumps(data, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_detector_health")


@tool
def check_apm_health(hours: int = 24) -> str:
    """
    Check the health of APM (Application Performance Monitoring) in the Splunk org.

    Assesses:
    - Service coverage (how many services are instrumented and reporting)
    - Error rate patterns across services and endpoints
    - Latency distribution and p99 trends
    - Trace ingestion health and sampling rates
    - Services that recently stopped reporting (silent service detection)

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
        return json.dumps(data, indent=2)
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        return summarise(rc, stdout, stderr, "check_apm_health")


@tool
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


@tool
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
