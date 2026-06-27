"""
Continuous watch loop — runs the agent on a schedule and emits summaries.
"""

import logging
import time
from datetime import datetime, timezone
from io import StringIO

logger = logging.getLogger(__name__)


def run_once(agent, config, prompt: str = None) -> str:
    """Run a single agent assessment. Returns the full agent output as a string."""
    default_prompt = (
        f"Run a full autonomous observability assessment for environment '{config.environment}'. "
        "Follow the standard sequence: check detector health, check APM health, analyze "
        "instrumentation gaps, scan cardinality, then provision or retune detectors as needed. "
        "Report everything you find with specific numbers and service names. "
        f"auto_apply mode is {'ENABLED — apply safe fixes automatically' if config.auto_apply else 'DISABLED — dry-run only, report recommendations'}."
    )

    output_buffer = StringIO()

    class BufferedAgent:
        """Wraps the Strands agent to capture output."""
        def __call__(self, msg):
            result = agent(msg)
            return result

    try:
        result = agent(prompt or default_prompt)
        # Strands returns an AgentResult; convert to string
        return str(result)
    except Exception as e:
        logger.error(f"Agent run failed: {e}", exc_info=True)
        return f"[Agent run failed at {datetime.now(timezone.utc).isoformat()}]: {e}"


def watch(
    agent,
    config,
    interval_minutes: int = 60,
    on_complete=None,
    max_runs: int = None,
) -> None:
    """
    Run the agent on a recurring schedule.

    Args:
        agent: Strands Agent instance from build_agent().
        config: AgentConfig instance.
        interval_minutes: How often to run (default: 60 minutes).
        on_complete: Optional callback(run_number, output, narrative) called after each run.
        max_runs: Stop after this many runs (None = run forever).
    """
    from narrator import generate_narrative

    run_count = 0
    logger.info(
        f"Starting watch loop — environment={config.environment}, "
        f"interval={interval_minutes}m, auto_apply={config.auto_apply}"
    )

    while True:
        run_count += 1
        start = datetime.now(timezone.utc)
        logger.info(f"[Run {run_count}] Starting at {start.isoformat()}")

        output = run_once(agent, config)
        narrative = generate_narrative(output, config)

        logger.info(f"[Run {run_count}] Completed in {(datetime.now(timezone.utc) - start).seconds}s")
        print(f"\n{'='*70}")
        print(f"  AUTONOMOUS O11Y AGENT  |  Run {run_count}  |  {start.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'='*70}")
        print(narrative)
        print(f"{'='*70}\n")

        if on_complete:
            try:
                on_complete(run_count, output, narrative)
            except Exception as e:
                logger.warning(f"on_complete callback failed: {e}")

        if max_runs and run_count >= max_runs:
            logger.info(f"Reached max_runs={max_runs}, stopping.")
            break

        logger.info(f"[Run {run_count}] Next run in {interval_minutes} minutes.")
        time.sleep(interval_minutes * 60)
