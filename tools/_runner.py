"""Shared subprocess runner used by all tool modules."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def batch_run(
    tasks: list[tuple[list, Path]], timeout: int = None
) -> list[tuple[int, str, str]]:
    """
    Run multiple subprocesses in parallel. Each task is (cmd, cwd).
    Returns results in the same order as tasks.
    """
    cfg = get_config()
    t = timeout or cfg.subprocess_timeout

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {
            pool.submit(run, cmd, cwd, t): i
            for i, (cmd, cwd) in enumerate(tasks)
        }
        results: list[tuple | None] = [None] * len(tasks)
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = (1, "", str(exc))

    return results


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
