"""
Continuous watch loop — runs the coordinator on a schedule.
"""

import logging
import time
from datetime import datetime, timezone
from observability.self_monitor import SelfMonitor

logger = logging.getLogger(__name__)


def run_once(agent, config, prompt: str = None, monitor=None) -> str:
    """Run a single assessment with optional self-monitoring."""
    from observability.self_monitor import SelfMonitor
    mon = monitor or SelfMonitor.noop()
    try:
        with mon.assessment_span(config.environment, config.auto_apply):
            # Pass monitor directly so coordinator records metrics with real findings
            result = agent(config, prompt=prompt, monitor=mon)
        return result
    except Exception as e:
        logger.error("Assessment run failed: %s", e, exc_info=True)
        return f"[Assessment failed at {datetime.now(timezone.utc).isoformat()}]: {e}"


def watch(
    agent,
    config,
    interval_minutes: int = 60,
    on_complete=None,
    max_runs: int = None,
    enable_approval: bool = False,
    monitor: SelfMonitor = None,
) -> None:
    """
    Run the coordinator on a recurring schedule.

    enable_approval: when True and config.auto_apply is False, present
    recommended actions after each run for human review/approval.
    """
    from approval.workflow import ApprovalWorkflow
    approval = ApprovalWorkflow() if enable_approval and not config.auto_apply else None

    run_count = 0
    logger.info(
        "Starting watch loop — environment=%s, interval=%dm, auto_apply=%s",
        config.environment,
        interval_minutes,
        config.auto_apply,
    )

    while True:
        run_count += 1
        start = datetime.now(timezone.utc)
        logger.info("[Run %d] Starting at %s", run_count, start.isoformat())

        # Pre-flight credential check — skip the run rather than produce a blank assessment
        try:
            from providers import check_provider_health
            healthy, reason = check_provider_health(config)
            if not healthy:
                logger.warning("[Run %d] SKIPPED — %s", run_count, reason)
                print(f"\n[Run {run_count}] SKIPPED: {reason}\n")
                if max_runs and run_count >= max_runs:
                    break
                logger.info("[Run %d] Next run in %d minutes.", run_count, interval_minutes)
                time.sleep(interval_minutes * 60)
                continue
        except Exception as exc:
            logger.warning("[Run %d] Pre-flight check failed (%s), proceeding anyway.", run_count, exc)

        output = run_once(agent, config, monitor=monitor)
        elapsed = (datetime.now(timezone.utc) - start).seconds
        logger.info("[Run %d] Completed in %ds", run_count, elapsed)

        print(f"\n{'='*70}")
        print(
            f"  AUTONOMOUS O11Y AGENT  |  Run {run_count}  |  "
            f"{start.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        print(f"{'='*70}")
        print(output)
        print(f"{'='*70}\n")

        # Approval workflow — present recommended actions if in dry-run mode
        if approval:
            try:
                from state import load_state
                state = load_state(config.environment)
                if state.runs:
                    _run_approval_from_state(approval, state, config)
            except Exception as exc:
                logger.warning("Approval workflow error: %s", exc)

        if on_complete:
            try:
                on_complete(run_count, output)
            except Exception as e:
                logger.warning("on_complete callback failed: %s", e)

        if max_runs and run_count >= max_runs:
            logger.info("Reached max_runs=%d, stopping.", max_runs)
            break

        logger.info("[Run %d] Next run in %d minutes.", run_count, interval_minutes)
        time.sleep(interval_minutes * 60)


def _run_approval_from_state(approval, state, config):
    """Re-construct pending actions from the last run's state record."""
    from approval.workflow import PendingAction
    last = state.last_run()
    if not last or not last.critical_issues:
        return
    # Build minimal PendingAction list from critical issues in state
    actions = [
        PendingAction(
            index=i,
            domain=issue.split("]")[0].lstrip("[") if "]" in issue else "unknown",
            severity="critical",
            service="",
            description=issue,
            recommendation="Review and remediate manually or re-run with --auto-apply.",
        )
        for i, issue in enumerate(last.critical_issues[:10])
    ]
    if actions:
        approval.run({"_state_actions": actions}, config)
