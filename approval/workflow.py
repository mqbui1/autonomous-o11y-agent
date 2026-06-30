"""
Human-in-the-loop approval workflow.

In dry-run mode (auto_apply=False), findings include recommended actions that
a human should review before they're applied. This module:

1. Extracts recommended actions from SpecialistFindings
2. Presents them as a numbered list
3. Accepts selective approval via stdin (interactive) or a webhook (CI/pipeline mode)
4. Re-runs approved actions by calling the relevant tool functions directly

Webhook mode: POST {"approved": [1, 3, 5]} to APPROVAL_WEBHOOK_URL, or set
APPROVAL_TIMEOUT_SECONDS to auto-approve all after N seconds with no response.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class PendingAction:
    index: int
    domain: str
    severity: str
    service: str
    description: str
    recommendation: str
    # Optional: direct callable to apply this action
    apply_fn: Callable | None = field(default=None, repr=False)


class ApprovalWorkflow:
    """
    Presents recommended actions from a dry-run assessment and applies
    the ones approved by a human operator.

    Modes:
      - interactive: reads approval from stdin (default when running in a terminal)
      - webhook: polls a webhook URL for approval response
      - auto: approves all actions automatically (useful for testing)
    """

    def __init__(
        self,
        webhook_url: str = "",
        timeout_seconds: int = 0,
        mode: str = "auto",
    ):
        self.webhook_url = webhook_url or os.environ.get("APPROVAL_WEBHOOK_URL", "")
        self.timeout_seconds = timeout_seconds or int(
            os.environ.get("APPROVAL_TIMEOUT_SECONDS", "0")
        )
        # mode: "interactive" | "webhook" | "auto"
        if mode == "auto":
            self.mode = "interactive" if sys.stdin.isatty() and not self.webhook_url else "auto"
        else:
            self.mode = mode
        if self.webhook_url:
            self.mode = "webhook"

    def run(self, findings: dict, config) -> list[PendingAction]:
        """
        Extract pending actions from findings, present them, get approval,
        and apply approved ones. Returns the list of applied actions.
        """
        actions = _extract_actions(findings)
        if not actions:
            logger.info("No recommended actions to review.")
            return []

        _print_actions(actions, config)

        if self.mode == "auto":
            logger.info("Auto-approval mode: applying all %d recommended actions.", len(actions))
            approved_indices = list(range(len(actions)))
        elif self.mode == "interactive":
            approved_indices = _prompt_interactive(actions)
        else:  # webhook
            approved_indices = _wait_for_webhook(self.webhook_url, len(actions), self.timeout_seconds)

        approved = [actions[i] for i in approved_indices if i < len(actions)]
        if not approved:
            print("\nNo actions approved. Run completed in review-only mode.")
            return []

        print(f"\nApplying {len(approved)} approved action(s)...")
        applied = []
        for action in approved:
            if action.apply_fn:
                try:
                    result = action.apply_fn()
                    print(f"  [{action.index + 1}] ✓ {action.description[:80]}")
                    logger.info("Applied action %d: %s -> %s", action.index + 1, action.description, result[:100])
                    applied.append(action)
                except Exception as exc:
                    print(f"  [{action.index + 1}] ✗ Failed: {exc}")
                    logger.error("Action %d failed: %s", action.index + 1, exc)
            else:
                print(f"  [{action.index + 1}] (no apply function — manual action required)")
                print(f"      → {action.recommendation}")

        return applied


def _get_tool_registry() -> dict:
    """Lazily load all tool functions for apply_fn reconnection."""
    try:
        from tools.provisioner import TOOL_FNS as P
        from tools.governance import TOOL_FNS as G
        from tools.health_check import TOOL_FNS as H
        from tools.analyzer import TOOL_FNS as A
        from tools.log_analyzer import TOOL_FNS as L
        from tools.rum_analyzer import TOOL_FNS as R
        from tools.dashboard import TOOL_FNS as D
        return {**P, **G, **H, **A, **L, **R, **D}
    except Exception as exc:
        logger.warning("Tool registry load failed: %s", exc)
        return {}


def _extract_actions(findings: dict) -> list[PendingAction]:
    """Extract HIGH and CRITICAL issues as pending actions from specialist findings."""
    actions = []
    idx = 0
    tool_registry = _get_tool_registry()
    domain_order = ["health", "instrumentation", "governance", "detector", "logs", "rum"]
    for domain in domain_order:
        f = findings.get(domain)
        if not f or not hasattr(f, "issues"):
            continue
        for issue in f.issues:
            if issue.severity in ("critical", "high"):
                # Reconnect apply_fn from action_tool + action_args if available
                apply_fn = None
                if getattr(issue, "action_tool", "") and issue.action_tool in tool_registry:
                    fn = tool_registry[issue.action_tool]
                    args = dict(getattr(issue, "action_args", {}) or {})
                    apply_fn = lambda f=fn, a=args: f(**a)

                actions.append(PendingAction(
                    index=idx,
                    domain=domain,
                    severity=issue.severity,
                    service=issue.service or "",
                    description=issue.description,
                    recommendation=issue.recommendation,
                    apply_fn=apply_fn,
                ))
                idx += 1
    return actions


def _print_actions(actions: list[PendingAction], config) -> None:
    sev_color = {"critical": "\033[91m", "high": "\033[93m"}
    reset = "\033[0m"
    print(f"\n{'='*70}")
    print(f"  RECOMMENDED ACTIONS — {config.environment}  (dry-run mode)")
    print(f"{'='*70}")
    print(f"  {len(actions)} action(s) found. Review and approve below.\n")
    for action in actions:
        color = sev_color.get(action.severity, "")
        svc = f" [{action.service}]" if action.service else ""
        print(f"  [{action.index + 1}] {color}[{action.severity.upper()}]{reset}{svc} {action.description}")
        print(f"       → {action.recommendation}\n")
    print(f"{'='*70}")


def _prompt_interactive(actions: list[PendingAction]) -> list[int]:
    """Ask operator which actions to approve. Returns list of 0-based indices."""
    print(
        "\nEnter action numbers to approve (comma-separated), "
        "'all' to approve all, or 'none' to skip: ",
        end="",
    )
    try:
        raw = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return []

    if raw == "all":
        return list(range(len(actions)))
    if raw in ("none", ""):
        return []
    indices = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            n = int(part) - 1  # convert 1-based to 0-based
            if 0 <= n < len(actions):
                indices.append(n)
    return indices


def _wait_for_webhook(webhook_url: str, count: int, timeout: int) -> list[int]:
    """
    POST the pending action list to webhook_url and wait for a response
    containing {"approved": [1, 3, ...]} (1-based indices).
    Falls back to auto-approving all after timeout_seconds (0 = no timeout).
    """
    approved_ref: list[list[int]] = [None]
    done = threading.Event()

    def poll():
        payload = json.dumps({"pending_count": count, "awaiting_approval": True}).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
                approved_ref[0] = [i - 1 for i in body.get("approved", [])]
                done.set()
        except Exception as exc:
            logger.warning("Approval webhook error: %s", exc)
            done.set()

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    done.wait(timeout=timeout or None)

    if approved_ref[0] is not None:
        return approved_ref[0]
    if timeout:
        logger.info("Approval timeout (%ds) reached — auto-approving all.", timeout)
        return list(range(count))
    return []
