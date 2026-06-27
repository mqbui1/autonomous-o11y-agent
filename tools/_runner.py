"""Shared subprocess runner used by all tool modules."""

import subprocess
import sys
from pathlib import Path

# Module-level config — set by agent.configure() before any tool is called
_config = None


def get_config():
    if _config is None:
        raise RuntimeError("Agent not configured. Call agent.configure(config) first.")
    return _config


def run(cmd: list, cwd: Path, timeout: int = None, extra_env: dict = None) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    import os
    env = os.environ.copy()
    env["SPLUNK_ACCESS_TOKEN"] = get_config().token
    env["SPLUNK_REALM"] = get_config().realm
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [sys.executable] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout or get_config().subprocess_timeout,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def summarise(returncode: int, stdout: str, stderr: str, tool_name: str) -> str:
    """Format subprocess output into a clean string for the LLM."""
    lines = []
    if stdout.strip():
        lines.append(stdout.strip())
    if returncode != 0 and stderr.strip():
        lines.append(f"\n[{tool_name} stderr]:\n{stderr.strip()[:2000]}")
    if not lines:
        lines.append(f"[{tool_name}] completed with no output (exit {returncode})")
    return "\n".join(lines)
