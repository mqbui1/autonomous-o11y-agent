"""
Cardinality governance tool — wraps o11y-usage-governance/cardinality_governance.py.
"""

from strands import tool
from ._runner import get_config, run, summarise


@tool
def scan_cardinality(top: int = 20, verbose: bool = False) -> str:
    """
    Scan the Splunk org for metric cardinality explosions and cost anomalies.
    Returns a ranked table of high-cardinality metrics with MTS counts, cost estimates,
    trend direction, instrumentation source, and the worst offending dimension for each.

    Metrics growing faster than their 7-day baseline are flagged as [ANOMALY Nx].

    Use this as the first step in telemetry pipeline governance. Follow up with
    fix_cardinality_report to get ready-to-apply OTel Collector YAML fixes.

    Args:
        top: Number of top metrics to return (default: 20).
        verbose: If True, include all severity levels including LOW.
    """
    cfg = get_config()
    cmd = ["cardinality_governance.py", "scan", "--top", str(top)]
    if verbose:
        cmd.append("--verbose")

    rc, stdout, stderr = run(cmd, cwd=cfg.governance_path)
    return summarise(rc, stdout, stderr, "scan_cardinality")


@tool
def scan_cardinality_anomalies(ratio: float = 2.0, days: int = 7) -> str:
    """
    Find metrics growing faster than their own historical baseline — catches slow-burn
    cardinality explosions before they cross static severity thresholds.

    This is more sensitive than scan_cardinality for detecting early-stage problems.
    A metric at 400 MTS growing 4x/week will hit 50,000 MTS within two weeks.

    Args:
        ratio: Flag metrics where current MTS >= ratio × baseline average (default: 2.0).
        days: Baseline window in days (default: 7).
    """
    cfg = get_config()
    cmd = [
        "cardinality_governance.py", "anomaly-scan",
        "--ratio", str(ratio),
        "--days", str(days),
    ]

    rc, stdout, stderr = run(cmd, cwd=cfg.governance_path)
    return summarise(rc, stdout, stderr, "scan_cardinality_anomalies")


@tool
def fix_cardinality_report(top: int = 20, no_ai: bool = False) -> str:
    """
    Generate a detailed cardinality report with ready-to-paste OTel Collector processor
    YAML fixes for each high-cardinality metric. Each fix targets the specific dimension
    causing the explosion (e.g. delete_key(server.address)).

    The report includes:
    - Per-metric dimension breakdown (which dimension is the root cause)
    - Drop YAML (eliminates the cardinality explosion completely)
    - Hash YAML alternative (preserves groupability while fixing cardinality)
    - Cost savings estimate per fix
    - Per-service and instrumentation source breakdown

    Args:
        top: Number of top metrics to analyze (default: 20).
        no_ai: If True, skip AI remediation narrative (faster).
    """
    cfg = get_config()
    cmd = [
        "cardinality_governance.py", "report",
        "--top", str(top),
        "--format", "md",
    ]
    if no_ai:
        cmd.append("--no-ai")

    rc, stdout, stderr = run(cmd, cwd=cfg.governance_path)
    return summarise(rc, stdout, stderr, "fix_cardinality_report")


@tool
def drilldown_dimension(dimension: str) -> str:
    """
    Get the full blast radius of a specific dimension across all metrics.
    Use this before applying a fix to confirm how many metrics are affected
    and to verify the generated YAML covers every impacted metric.

    Args:
        dimension: Dimension name to drill down on (e.g. "server.address", "request_id").
    """
    cfg = get_config()
    cmd = ["cardinality_governance.py", "drilldown", "--dimension", dimension]

    rc, stdout, stderr = run(cmd, cwd=cfg.governance_path)
    return summarise(rc, stdout, stderr, "drilldown_dimension")


@tool
def scan_trace_volume(lookback_hours: float = 1.0) -> str:
    """
    Snapshot current per-service APM span volumes. Use this to detect which services
    are generating unexpected trace volume spikes. Results are saved to history for
    later comparison with compare_trace_volume.

    Args:
        lookback_hours: Hours of trace history to sample (default: 1.0).
    """
    cfg = get_config()
    cmd = [
        "cardinality_governance.py", "trace-scan",
        "--lookback", str(lookback_hours),
    ]
    if cfg.environment:
        cmd.extend(["--environment", cfg.environment])

    rc, stdout, stderr = run(cmd, cwd=cfg.governance_path)
    return summarise(rc, stdout, stderr, "scan_trace_volume")
