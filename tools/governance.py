"""
Cardinality governance tool — wraps o11y-usage-governance/cardinality_governance.py.
"""

from ._runner import get_config, run, batch_run, summarise


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


def full_cardinality_scan(top: int = 20, ratio: float = 2.0, days: int = 7) -> str:
    """
    Run cardinality scan AND anomaly scan in parallel — more efficient than calling
    each separately. Returns both results in a single response.

    Args:
        top: Number of top metrics to return for the cardinality scan (default: 20).
        ratio: Flag metrics where current MTS >= ratio × baseline (default: 2.0).
        days: Baseline window in days for anomaly detection (default: 7).
    """
    cfg = get_config()
    tasks = [
        (["cardinality_governance.py", "scan", "--top", str(top)], cfg.governance_path),
        (
            [
                "cardinality_governance.py", "anomaly-scan",
                "--ratio", str(ratio),
                "--days", str(days),
            ],
            cfg.governance_path,
        ),
    ]
    results = batch_run(tasks)
    rc0, out0, err0 = results[0]
    rc1, out1, err1 = results[1]
    parts = [
        "### Cardinality Scan\n" + summarise(rc0, out0, err0, "scan_cardinality"),
        "### Anomaly Scan\n" + summarise(rc1, out1, err1, "scan_cardinality_anomalies"),
    ]
    return "\n\n".join(parts)


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


SCHEMAS = [
    {
        "toolSpec": {
            "name": "full_cardinality_scan",
            "description": (
                "Run cardinality scan AND anomaly scan in parallel in a single call. "
                "Prefer this over calling scan_cardinality and scan_cardinality_anomalies separately."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "top": {
                            "type": "integer",
                            "description": "Number of top metrics to return (default: 20)",
                        },
                        "ratio": {
                            "type": "number",
                            "description": "Flag metrics where current MTS >= ratio × baseline (default: 2.0)",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Baseline window in days (default: 7)",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "scan_cardinality",
            "description": (
                "Scan for metric cardinality explosions and cost anomalies. Returns a ranked "
                "table of high-cardinality metrics with MTS counts, cost estimates, trend "
                "direction, and the worst offending dimension for each."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "top": {
                            "type": "integer",
                            "description": "Number of top metrics to return (default: 20)",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": "If true, include LOW severity findings",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "scan_cardinality_anomalies",
            "description": (
                "Find metrics growing faster than their historical baseline — catches "
                "slow-burn cardinality explosions before they hit static thresholds."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "ratio": {
                            "type": "number",
                            "description": "Flag metrics where current MTS >= ratio × baseline (default: 2.0)",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Baseline window in days (default: 7)",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "fix_cardinality_report",
            "description": (
                "Generate a detailed cardinality report with ready-to-paste OTel Collector "
                "processor YAML fixes. Call after scan_cardinality finds issues."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "top": {
                            "type": "integer",
                            "description": "Number of top metrics to analyze (default: 20)",
                        },
                        "no_ai": {
                            "type": "boolean",
                            "description": "If true, skip AI remediation narrative (faster)",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "drilldown_dimension",
            "description": (
                "Get the full blast radius of a specific dimension across all metrics. "
                "Use before applying a fix to confirm scope."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "dimension": {
                            "type": "string",
                            "description": "Dimension name to drill down on (e.g. 'server.address')",
                        }
                    },
                    "required": ["dimension"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "scan_trace_volume",
            "description": (
                "Snapshot current per-service APM span volumes to detect unexpected traffic spikes."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "lookback_hours": {
                            "type": "number",
                            "description": "Hours of trace history to sample (default: 1.0)",
                        }
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "full_cardinality_scan": full_cardinality_scan,
    "scan_cardinality": scan_cardinality,
    "scan_cardinality_anomalies": scan_cardinality_anomalies,
    "fix_cardinality_report": fix_cardinality_report,
    "drilldown_dimension": drilldown_dimension,
    "scan_trace_volume": scan_trace_volume,
}
