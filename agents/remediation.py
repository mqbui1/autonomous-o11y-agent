"""
Remediation specialist — generates actionable fix plans from assessment findings.

Pure Python, no LLM required. Each critical/high Issue is mapped to a
supervisor ActionEngine payload (action_type + action_payload) using
rule-based pattern matching on issue descriptions.

The generated pending_remediations list is saved in the assessment JSON
and surfaced in the supervisor UI for selective approval and application.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def generate_remediations(
    findings: dict,
    splunk_config: dict | None = None,
    environment: str = "",
) -> list[dict[str, Any]]:
    """
    Translate assessment findings into a list of pending remediations.

    Each remediation dict:
      id             — stable 8-char hash ID (action_type + service + topic)
      severity       — critical | high
      domain         — source specialist domain
      service        — affected service (empty = org-wide)
      description    — human-readable issue text from the specialist
      recommendation — what to fix
      action_type    — supervisor ActionEngine action type
      action_payload — args for the ActionEngine handler
      auto_applicable — True if the action can run without manual config edits
      status         — always "pending" on creation
    """
    remediations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for domain, f in findings.items():
        for issue in getattr(f, "issues", []):
            if issue.severity not in ("critical", "high"):
                continue
            action = _infer_action(issue, domain, splunk_config or {}, environment)
            if action is None:
                continue
            # Deduplicate: same action_type + service + topic → single remediation
            fp = f"{action['action_type']}|{(issue.service or '').lower()}|{action['_topic']}"
            if fp in seen:
                continue
            seen.add(fp)
            rem_id = "rem_" + hashlib.md5(fp.encode()).hexdigest()[:8]
            remediations.append({
                "id": rem_id,
                "severity": issue.severity,
                "domain": domain,
                "service": issue.service or "",
                "description": issue.description,
                "recommendation": issue.recommendation or action["description"],
                "action_type": action["action_type"],
                "action_payload": action["payload"],
                "auto_applicable": action.get("auto_applicable", True),
                "status": "pending",
            })

    remediations.sort(key=lambda r: (0 if r["severity"] == "critical" else 1, r["service"], r["domain"]))
    return remediations


def _infer_action(
    issue: Any,
    domain: str,
    splunk_config: dict,
    environment: str,
) -> dict[str, Any] | None:
    """Return an ActionEngine invocation dict for this issue, or None if not actionable."""
    desc = (issue.description or "").lower()
    svc = (issue.service or "").strip()
    spl = splunk_config

    # ── Detector provisioning ─────────────────────────────────────────────────
    if (
        _match(desc, ["no detector", "zero detector", "no alert", "no automated alert",
                      "dark service", "detector coverage", "uncovered", "without.*detector",
                      "deploy.*detector", "deploy error-rate detector"])
        or getattr(issue, "action_tool", "") == "provision_detectors"
        or domain == "detector"
    ):
        if svc:
            return {
                "action_type": "create_splunk_detector",
                "payload": {
                    "service": svc,
                    "types": ["error_rate", "latency"],
                    "environment": environment,
                    "splunk_config": spl,
                },
                "description": f"Deploy error-rate and latency detectors for {svc}",
                "auto_applicable": True,
                "_topic": "detector",
            }
        return {
            "action_type": "build_detectors",
            "payload": {"dry_run": False, "environment": environment, "splunk_config": spl},
            "description": "Build detectors for all dark/uncovered services",
            "auto_applicable": True,
            "_topic": "detector_all",
        }

    # ── OTel Collector problems ───────────────────────────────────────────────
    if _match(desc, ["collector unreachable", "otelcol", "otel collector", "collector down"]):
        return {
            "action_type": "reload_collector",
            "payload": {},
            "description": "Reload the OTel Collector (rolling restart or SIGHUP)",
            "auto_applicable": True,
            "_topic": "collector",
        }

    # ── Performance / code-level fix (from performance specialist) ───────────────
    # Only map to generate_code_fix when action_tool is explicitly set AND the
    # specialist provided actual code location data (file or function). Without
    # those, it's a mislabeled config/instrumentation finding — drop it so it
    # doesn't appear as a meaningless manual "Code Fix" in the UI.
    if getattr(issue, "action_tool", "") == "generate_code_fix":
        args = getattr(issue, "action_args", {}) or {}
        if not args.get("file") and not args.get("function") and not args.get("pattern"):
            return None  # no code location — not a real code fix
        return {
            "action_type": "generate_code_fix",
            "payload": {
                "service": svc,
                "file": args.get("file", ""),
                "line": args.get("line", 0),
                "function": args.get("function", ""),
                "pattern": args.get("pattern", ""),
                "db_system": args.get("db_system", ""),
                "suggested_diff": args.get("suggested_diff", ""),
                "fix_description": args.get("fix_description", issue.description),
            },
            "description": args.get("fix_description") or issue.description,
            "auto_applicable": False,  # always human-reviewed — LLM-generated code
            "_topic": f"perf_{args.get('pattern', 'hotspot')}_{(args.get('file', '') or svc)[-20:]}",
        }

    # ── DB instrumentation gap (services never had db.* attrs — OTel library missing) ──
    if domain == "db" and _match(desc, [
        "missing db instrumentation", "db instrumentation", "db blind spot",
        "db.system, db.name, db.operation", "db technology", "db calls with no db",
    ]):
        db_systems = getattr(issue, "action_args", {}).get("db_systems", [])
        missing_attrs = getattr(issue, "action_args", {}).get(
            "missing_attributes", ["db.system", "db.name", "db.operation"]
        )
        return {
            "action_type": "add_db_instrumentation",
            "payload": {
                "service": svc,
                "db_systems": db_systems,
                "missing_attributes": missing_attrs,
            },
            "description": (
                f"Add OTel DB instrumentation for {svc} — "
                f"db_systems: {db_systems or ['unknown']} — "
                f"missing: {missing_attrs}"
            ),
            "auto_applicable": True,
            "_topic": "db_instrumentation",
        }

    # ── DB span attributes stripped by collector processor ────────────────────
    if _match(desc, ["strip_db_attrs", "db attr stripped", "db span attribute stripped",
                     "transform/strip", "collector stripping"]):
        return {
            "action_type": "patch_collector_config",
            "payload": {
                "config_change": "remove_processor",
                "processor_name": "transform/strip_db_attrs",
                "description": (
                    "Remove transform/strip_db_attrs processor from OTel Collector traces pipeline "
                    "to restore database visibility (db.system, db.name, db.operation attributes)."
                ),
            },
            "description": "Remove transform/strip_db_attrs to restore DB span attributes",
            "auto_applicable": False,
            "_topic": "db_attrs",
        }

    # ── Service restart (silent service) ─────────────────────────────────────
    if svc and _match(desc, ["silent", "no telemetry", "not reporting", "stopped reporting"]):
        return {
            "action_type": "restart_service",
            "payload": {"service_name": svc},
            "description": f"Restart {svc} — service has gone silent",
            "auto_applicable": True,
            "_topic": "service_silence",
        }

    # ── Detector rebaseline ───────────────────────────────────────────────────
    if svc and _match(desc, ["threshold too", "noisy detector", "detector threshold"]):
        return {
            "action_type": "rebaseline_detectors",
            "payload": {"services": [svc], "splunk_config": spl},
            "description": f"Rebaseline detector thresholds for {svc} (7-day mean+2σ)",
            "auto_applicable": True,
            "_topic": "rebaseline",
        }

    return None


def _match(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)
