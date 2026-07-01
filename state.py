"""
Persistent run state per environment.

Stored at ~/.o11y-agent/{environment}.json
Tracks the last MAX_RUNS assessments so the agent can reason about trends,
regressions, and whether previous recommendations were acted on.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".o11y-agent"
MAX_RUNS = 30


@dataclass
class RunRecord:
    timestamp: str
    # Counts
    instrumentation_score: int | None
    services_active: int
    services_silent: int
    detector_count: int
    cardinality_issues: int
    # Named lists — used for feedback loop (Gap 7)
    active_service_names: list[str] = field(default_factory=list)
    silent_service_names: list[str] = field(default_factory=list)
    deployed_detector_ids: list[str] = field(default_factory=list)
    critical_issues: list[str] = field(default_factory=list)   # issue descriptions
    actions_taken: list[str] = field(default_factory=list)     # audit trail
    top_findings: list[str] = field(default_factory=list)


@dataclass
class EnvironmentState:
    environment: str
    runs: list[RunRecord] = field(default_factory=list)

    def last_run(self) -> RunRecord | None:
        return self.runs[-1] if self.runs else None

    def trend_context(self) -> str:
        """
        Text summary of prior runs for injection into each specialist's context.
        Surfaces trend deltas, prior silent services, and prior deployed detectors
        so agents can explicitly verify whether previous findings were resolved (Gap 7).
        """
        if not self.runs:
            return ""

        last = self.runs[-1]
        lines = [
            f"## Prior run history for `{self.environment}` ({len(self.runs)} run(s) recorded)\n",
            f"**Last run:** {last.timestamp}",
        ]

        if last.instrumentation_score is not None:
            lines.append(f"- Instrumentation score: {last.instrumentation_score}/100")
        lines.append(
            f"- Active services: {last.services_active}, Silent: {last.services_silent}"
        )
        lines.append(f"- Deployed detectors: {last.detector_count}")
        if last.cardinality_issues:
            lines.append(f"- Cardinality issues: {last.cardinality_issues}")

        # Feedback loop: explicitly ask agents to verify prior state (Gap 7)
        if last.silent_service_names:
            names = ", ".join(f"`{s}`" for s in last.silent_service_names[:10])
            lines.append(
                f"\n**Previously silent services (verify if now active):** {names}"
            )
        if last.deployed_detector_ids:
            ids = ", ".join(last.deployed_detector_ids[:10])
            lines.append(
                f"\n**Detectors deployed last run (verify still present and healthy):** {ids}"
            )
        if last.critical_issues:
            lines.append("\n**Critical issues from last run (confirm if resolved):**")
            for issue in last.critical_issues[:5]:
                lines.append(f"  - {issue}")
        if last.actions_taken:
            lines.append("\n**Actions taken last run (verify they held):**")
            for action in last.actions_taken[:8]:
                lines.append(f"  - {action}")

        # Score trend
        if len(self.runs) >= 2:
            prev = self.runs[-2]
            if (
                last.instrumentation_score is not None
                and prev.instrumentation_score is not None
            ):
                delta = last.instrumentation_score - prev.instrumentation_score
                direction = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                lines.append(
                    f"\n**Instrumentation score trend:** "
                    f"{prev.instrumentation_score} → {last.instrumentation_score} "
                    f"({direction}{abs(delta)} points)"
                )

        # Consecutive silence tracking
        if last.silent_service_names and len(self.runs) >= 2:
            prev = self.runs[-2]
            persistent = set(last.silent_service_names) & set(prev.silent_service_names)
            if persistent:
                names = ", ".join(f"`{s}`" for s in sorted(persistent))
                lines.append(
                    f"\n**Persistently silent (2+ consecutive runs):** {names} "
                    "— likely a live instrumentation failure, not a never-active service."
                )

        lines.append(
            "\nUse this context to identify regressions, confirm improvements, "
            "and explicitly verify that previously silent services and deployed detectors "
            "are in the expected state."
        )
        return "\n".join(lines)

    def add_run(self, record: RunRecord):
        self.runs.append(record)
        if len(self.runs) > MAX_RUNS:
            self.runs = self.runs[-MAX_RUNS:]


def load_state(environment: str) -> EnvironmentState:
    path = STATE_DIR / f"{environment}.json"
    if not path.exists():
        return EnvironmentState(environment=environment)
    try:
        data = json.loads(path.read_text())
        runs = [RunRecord(**r) for r in data.get("runs", [])]
        return EnvironmentState(environment=environment, runs=runs)
    except Exception as exc:
        logger.warning(
            "Could not load state for %s: %s — starting fresh", environment, exc
        )
        return EnvironmentState(environment=environment)


def save_state(state: EnvironmentState):
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{state.environment}.json"
    path.write_text(json.dumps(asdict(state), indent=2))
    logger.debug("State saved: %s", path)


# ── Assessment detail (full per-specialist findings + synthesis) ──────────────

def save_assessment_detail(environment: str, detail: dict) -> None:
    """Overwrite the latest full assessment detail for an environment."""
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{environment}_detail.json"
    path.write_text(json.dumps(detail, indent=2))
    logger.debug("Assessment detail saved: %s", path)


def load_assessment_detail(environment: str) -> dict | None:
    """Return the latest full assessment detail, or None if none exists."""
    path = STATE_DIR / f"{environment}_detail.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Could not load assessment detail for %s: %s", environment, exc)
        return None


def build_run_record(
    environment: str,
    findings: dict,  # dict[str, SpecialistFindings]
) -> RunRecord:
    """
    Build a RunRecord directly from structured SpecialistFindings (Gap 3).
    No regex — data comes from the submit_findings schema fields.
    """
    health = findings.get("health")
    instrumentation = findings.get("instrumentation")
    governance = findings.get("governance")
    detector = findings.get("detector")

    # Instrumentation score
    score = None
    if instrumentation and instrumentation.instrumentation_score is not None:
        score = instrumentation.instrumentation_score
    elif instrumentation and instrumentation.metrics.get("score") is not None:
        score = instrumentation.metrics["score"]

    # Silent / active service counts + names
    silent_names: list[str] = []
    active_names: list[str] = []
    for f in findings.values():
        if hasattr(f, "services_silent"):
            silent_names.extend(f.services_silent)
        if hasattr(f, "services_active"):
            active_names.extend(f.services_active)
    silent_names = list(dict.fromkeys(silent_names))  # deduplicate, preserve order
    active_names = list(dict.fromkeys(active_names))
    active_count = len(active_names)
    silent_count = len(silent_names)

    # Deployed detector IDs (Gap 7 feedback loop)
    deployed_ids: list[str] = []
    detector_count = 0
    if detector:
        deployed_ids = detector.metrics.get("deployed_ids", [])
        detector_count = detector.metrics.get("deployed_count", len(deployed_ids))

    # Cardinality issues
    cardinality_issues = 0
    if governance:
        cardinality_issues = governance.metrics.get("anomaly_count", 0) + (
            1 if governance.metrics.get("top_cardinality_mts", 0) > 10000 else 0
        )

    # Critical issues (descriptions only)
    critical_issues: list[str] = []
    for f in findings.values():
        if hasattr(f, "issues"):
            for issue in f.issues:
                if issue.severity == "critical":
                    svc = f" [{issue.service}]" if issue.service else ""
                    critical_issues.append(f"[{f.domain}]{svc} {issue.description}")

    # Audit trail — collect actions actually taken across all specialists
    all_actions: list[str] = []
    for f in findings.values():
        if hasattr(f, "actions_taken"):
            all_actions.extend(f.actions_taken)

    return RunRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        instrumentation_score=score,
        services_active=active_count,
        services_silent=silent_count,
        active_service_names=active_names,
        silent_service_names=silent_names,
        detector_count=detector_count,
        deployed_detector_ids=deployed_ids,
        cardinality_issues=cardinality_issues,
        critical_issues=critical_issues,
        actions_taken=all_actions,
        top_findings=[f.summary for f in findings.values() if hasattr(f, "summary")],
    )
