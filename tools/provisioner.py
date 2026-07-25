"""
Detector provisioner tool — wraps auto-detector-provisioner/provision.py.
"""

from pathlib import Path

from ._runner import get_config, run, summarise

_SCRIPT = "provision.py"

# provision.py writes baseline cache files (data/baselines/<env>__<service>.json) and
# state (data/provisioned_state.json) relative to its own process cwd. deploy/docker-
# compose.yml bind-mounts cfg.provisioner_path READ-ONLY (it's a sibling project's
# source tree), so any service without an already-cached baseline crashes with
# "OSError: [Errno 30] Read-only file system: 'data/baselines/...'" — an UNCAUGHT
# exception in provision.py that aborts the whole run on the first affected service.
# Confirmed 2026-07-25 via direct reproduction (docker exec + manual provision.py
# invocation): this has been silently killing detector provisioning on every call for
# any service not already provisioned, hidden from both the model and log monitoring
# because tools/_runner.py's summarise() truncates stderr to 2000 chars — the discovery-
# phase logging that precedes the traceback in provision.py's own stderr output eats up
# that whole budget before the actual error ever appears. Same fix as tools/governance.py:
# run from a writable cwd, passing the script itself as an absolute path.
_WRITABLE_CWD = Path.home() / ".o11y-agent" / "provisioner"


def _script_path(cfg) -> str:
    return str(cfg.provisioner_path / _SCRIPT)


def _writable_cwd() -> Path:
    _WRITABLE_CWD.mkdir(parents=True, exist_ok=True)
    return _WRITABLE_CWD


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
        _script_path(cfg),
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

    rc, stdout, stderr = run(cmd, cwd=_writable_cwd(), timeout=cfg.provision_timeout)
    return summarise(rc, stdout, stderr, "provision_detectors")


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
        _script_path(cfg),
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--environment", cfg.environment,
        "--retune",
    ]
    if cfg.auto_apply:
        cmd.append("--auto-deploy")
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])

    rc, stdout, stderr = run(cmd, cwd=_writable_cwd(), timeout=cfg.provision_timeout)
    return summarise(rc, stdout, stderr, "retune_detectors")


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
        _script_path(cfg),
        "--realm", cfg.realm,
        "--token", cfg.token,
        "--environment", cfg.environment,
        "--audit",
    ]
    if service or cfg.service:
        cmd.extend(["--service", service or cfg.service])

    rc, stdout, stderr = run(cmd, cwd=_writable_cwd())
    return summarise(rc, stdout, stderr, "audit_detectors")


SCHEMAS = [
    {
        "toolSpec": {
            "name": "provision_detectors",
            "description": (
                "Discover services in the configured environment, learn behavioral baselines "
                "from live telemetry, and provision best-practice detectors tuned to actual "
                "behavior. Supports JVM, Python, Node.js, Go, .NET, Rust, GenAI/agentic, "
                "Kubernetes, Istio, Redis, PostgreSQL, and 30+ other stacks."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "auto_deploy": {
                            "type": "boolean",
                            "description": "If true, deploy to Splunk. If false, dry-run only.",
                        },
                        "service": {
                            "type": "string",
                            "description": "Scope to a specific service. Leave empty for all.",
                        },
                        "skip_baseline": {
                            "type": "boolean",
                            "description": "If true, use fixed industry thresholds instead of learned baselines.",
                        },
                        "baseline_window_hours": {
                            "type": "integer",
                            "description": "Hours of history for baseline learning (default: 168 = 7 days).",
                        },
                        "reconcile": {
                            "type": "boolean",
                            "description": "If true, update changed detectors in-place instead of recreating.",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "retune_detectors",
            "description": (
                "Recompute baselines from recent telemetry and update existing detector "
                "thresholds. Use when detectors are too noisy or missing real incidents."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Scope to a specific service. Leave empty for all.",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "audit_detectors",
            "description": (
                "Audit deployed detectors for effectiveness. Identifies never-fired and "
                "excessively noisy detectors for the configured environment."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Scope to a specific service. Leave empty for all.",
                        }
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "provision_detectors": provision_detectors,
    "retune_detectors": retune_detectors,
    "audit_detectors": audit_detectors,
}
