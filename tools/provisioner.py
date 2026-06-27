"""
Detector provisioner tool — wraps auto-detector-provisioner/provision.py.
"""

from strands import tool
from ._runner import get_config, run, summarise


@tool
def provision_detectors(
    auto_deploy: bool = False,
    service: str = "",
    skip_baseline: bool = False,
    baseline_window_hours: int = 168,
    reconcile: bool = False,
) -> str:
    """
    Discover services in the configured Splunk environment, detect their tech stack
    (JVM, Python, Node.js, Go, .NET, Rust, GenAI, Kubernetes, AWS, and 30+ frameworks),
    learn behavioral baselines from live telemetry, and provision or retune
    best-practice detectors tuned to actual application behavior.

    Use this tool to:
    - Discover what services exist in the environment and what detectors they are missing
    - Generate detector recommendations with dynamic thresholds tuned to real baselines
    - Deploy detectors to Splunk Observability Cloud (set auto_deploy=True)
    - Retune existing detectors when baselines have drifted (set reconcile=True)

    GenAI/Agentic services are auto-detected and get specialized detectors for:
    LLM operation duration, token usage spikes, API error rate, context window saturation,
    tool call failures, response truncation, and agent invocation error rate.

    Args:
        auto_deploy: If True, deploy detectors to Splunk. If False, dry-run only.
        service: Scope to a specific service name. Leave empty for all services.
        skip_baseline: If True, use fixed industry-standard thresholds (faster, less accurate).
        baseline_window_hours: Hours of history to use for baseline learning (default: 168 = 7 days).
        reconcile: If True, update changed detectors in-place instead of recreating.
    """
    cfg = get_config()
    cmd = [
        "provision.py",
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--environment", cfg.environment,
    ]
    if auto_deploy or cfg.auto_apply:
        cmd.append("--auto-deploy")
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])
    if skip_baseline:
        cmd.append("--skip-baseline")
    if baseline_window_hours != 168:
        cmd.extend(["--baseline-window-hours", str(baseline_window_hours)])
    if reconcile:
        cmd.append("--reconcile")

    rc, stdout, stderr = run(cmd, cwd=cfg.provisioner_path)
    return summarise(rc, stdout, stderr, "provision_detectors")


@tool
def retune_detectors(service: str = "") -> str:
    """
    Recompute baselines from recent telemetry and update existing detector thresholds
    to match the application's current behavior. Use this when detectors are too noisy
    (thresholds too tight) or missing real incidents (thresholds too loose).

    Args:
        service: Scope retuning to a specific service. Leave empty for all services.
    """
    cfg = get_config()
    cmd = [
        "provision.py",
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--environment", cfg.environment,
        "--retune",
    ]
    if cfg.auto_apply:
        cmd.append("--auto-deploy")
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])

    rc, stdout, stderr = run(cmd, cwd=cfg.provisioner_path)
    return summarise(rc, stdout, stderr, "retune_detectors")


@tool
def audit_detectors(service: str = "") -> str:
    """
    Audit deployed detectors for effectiveness. Identifies detectors that have never
    fired (potentially redundant) and detectors that are too noisy (firing constantly).
    Use this to assess the quality of existing detector coverage.

    Args:
        service: Scope audit to a specific service. Leave empty for all services.
    """
    cfg = get_config()
    cmd = [
        "provision.py",
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--environment", cfg.environment,
        "--audit",
    ]
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])

    rc, stdout, stderr = run(cmd, cwd=cfg.provisioner_path)
    return summarise(rc, stdout, stderr, "audit_detectors")
