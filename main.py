#!/usr/bin/env python3
"""
Autonomous O11y Agent — single entrypoint.

Usage:
  # One-shot assessment (dry-run)
  python3 main.py --realm us1 --token $TOKEN --environment production

  # One-shot with auto-apply
  python3 main.py --realm us1 --token $TOKEN --environment production --auto-apply

  # Continuous watch mode (runs every 60 minutes)
  python3 main.py --realm us1 --token $TOKEN --environment production --watch

  # Scope to a specific service
  python3 main.py --realm us1 --token $TOKEN --environment production --service payment-service

  # Custom watch interval
  python3 main.py --realm us1 --token $TOKEN --environment production --watch --interval 30

  # Ask the agent a specific question
  python3 main.py --realm us1 --token $TOKEN --environment production \
      --prompt "Which services have the worst instrumentation coverage?"
"""

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Autonomous Observability Agent for Splunk Observability Cloud",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    conn = parser.add_argument_group("connection")
    conn.add_argument("--realm", default=os.environ.get("SPLUNK_REALM"),
                      help="Splunk Observability realm (e.g. us1, us0, eu0)")
    conn.add_argument("--token", default=os.environ.get("SPLUNK_ACCESS_TOKEN"),
                      help="Splunk API access token (or SPLUNK_ACCESS_TOKEN env var)")

    scope = parser.add_argument_group("scope")
    scope.add_argument("--environment", "--env", default=os.environ.get("SPLUNK_ENVIRONMENT"),
                       help="Target environment name")
    scope.add_argument("--service", default="",
                       help="Scope to a specific service (optional)")

    mode = parser.add_argument_group("mode")
    mode.add_argument("--auto-apply", action="store_true",
                      help="Apply safe fixes automatically (default: dry-run)")
    mode.add_argument("--watch", action="store_true",
                      help="Run continuously on a schedule")
    mode.add_argument("--interval", type=int, default=60, metavar="MINUTES",
                      help="Watch mode interval in minutes (default: 60)")
    mode.add_argument("--prompt", default=None, metavar="TEXT",
                      help="Custom prompt instead of the default full assessment")
    mode.add_argument("--no-narrative", action="store_true",
                      help="Skip narrative synthesis (faster, raw agent output only)")
    mode.add_argument("--streaming", action="store_true",
                      help="Start OTLP/HTTP receiver for gateway co-deployment (always-on mode)")
    mode.add_argument("--streaming-only", action="store_true",
                      help="Streaming receiver only — no batch assessment (useful for testing)")

    paths = parser.add_argument_group("tool paths (override defaults)")
    paths.add_argument("--provisioner-path", type=Path, default=None)
    paths.add_argument("--governance-path", type=Path, default=None)
    paths.add_argument("--analyzer-path", type=Path, default=None)
    paths.add_argument("--health-check-path", type=Path, default=None)

    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")

    return parser.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate required args
    missing = [name for name, val in [("--realm", args.realm), ("--token", args.token), ("--environment", args.environment)] if not val]
    if missing:
        print(f"Error: missing required arguments: {', '.join(missing)}", file=sys.stderr)
        print("Set via CLI flags or env vars: SPLUNK_REALM, SPLUNK_ACCESS_TOKEN, SPLUNK_ENVIRONMENT", file=sys.stderr)
        sys.exit(1)

    from config import AgentConfig
    from agent import build_agent
    from loop import run_once, watch

    config = AgentConfig(
        realm=args.realm,
        token=args.token,
        environment=args.environment,
        auto_apply=args.auto_apply,
        service=args.service,
    )
    if args.provisioner_path:
        config.provisioner_path = args.provisioner_path
    if args.governance_path:
        config.governance_path = args.governance_path
    if args.analyzer_path:
        config.analyzer_path = args.analyzer_path
    if args.health_check_path:
        config.health_check_path = args.health_check_path

    # Verify tool paths exist
    for name, path in [
        ("provisioner", config.provisioner_path),
        ("governance", config.governance_path),
        ("analyzer", config.analyzer_path),
        ("health-check", config.health_check_path),
    ]:
        if not path.exists():
            logger.warning(f"Tool path not found: {name} → {path}  (tool will fail if called)")

    logger.info(
        f"Autonomous O11y Agent starting  |  realm={config.realm}  "
        f"env={config.environment}  auto_apply={config.auto_apply}"
    )

    agent = build_agent(config)

    from observability.self_monitor import SelfMonitor
    monitor = SelfMonitor.from_config(config)

    if args.streaming or args.streaming_only:
        _run_streaming(agent, config, args, monitor=monitor)
        return  # streaming mode runs until killed

    if args.watch:
        watch(
            agent=agent,
            config=config,
            interval_minutes=args.interval,
            monitor=monitor,
            enable_approval=not config.auto_apply,
        )
    else:
        output = run_once(agent, config, prompt=args.prompt, monitor=monitor)

        print("\n" + "="*70)
        print("  AUTONOMOUS O11Y AGENT ASSESSMENT")
        print("="*70 + "\n")
        print(output)
        print("\n" + "="*70)


def _run_streaming(agent, config, args, monitor=None):
    """
    Always-on mode for gateway co-deployment.

    Starts the OTLP/HTTP receiver in a background thread, seeds it with
    known services from Splunk, then runs batch assessments on schedule
    (unless --streaming-only is set).
    """
    import time
    from streaming.pipeline import StreamingPipeline
    from receiver.otlp_receiver import start_receiver

    logger.info(
        "Starting streaming mode — OTLP receiver on %s:%d",
        config.streaming_host,
        config.streaming_port,
    )

    from streaming.observations import ObservationBuffer
    from state import load_state

    pipeline = StreamingPipeline.from_config(config)
    obs_buffer = ObservationBuffer(retention_minutes=120)
    pipeline.set_observation_buffer(obs_buffer)

    # Seed service tracker from last known state so existing services don't
    # trigger new-service provisioning callbacks on every restart.
    state = load_state(config.environment)
    if state.runs:
        last = state.last_run()
        known = list(last.active_service_names) + list(last.silent_service_names)
        if known:
            pipeline.service_tracker.seed(known)
            logger.info("Seeded service tracker with %d known services from state", len(known))

    # Register new-service callback: trigger detector provisioning
    def on_new_service(service: str):
        logger.info("New service '%s' detected — triggering detector provisioning", service)
        try:
            from tools.provisioner import TOOL_FNS
            result = TOOL_FNS["provision_detectors"](service=service)
            logger.info("Provisioning result for %s: %s", service, result[:200])
        except Exception as exc:
            logger.error("Provisioning failed for %s: %s", service, exc)

    pipeline.service_tracker.on_new_service(on_new_service)

    start_receiver(pipeline, port=config.streaming_port, host=config.streaming_host,
                   environment=config.environment)
    logger.info(
        "OTLP receiver ready. Configure gateway otlp/http exporter to "
        "http://<agent-service>:%d",
        config.streaming_port,
    )

    if args.streaming_only:
        logger.info("Streaming-only mode — no batch assessments. Running until killed.")
        while True:
            time.sleep(3600)
        return

    # Streaming + batch: run assessments on schedule alongside the receiver
    interval_minutes = args.interval if args.watch else 60
    run_count = 0
    logger.info("Batch assessments will run every %d minutes.", interval_minutes)

    while True:
        run_count += 1
        logger.info("[Batch run %d] Starting assessment", run_count)
        try:
            output = agent(config, prompt=args.prompt, observation_buffer=obs_buffer, monitor=monitor)
            # Re-seed service tracker after each batch run with updated service list
            updated_state = load_state(config.environment)
            if updated_state.runs:
                last = updated_state.last_run()
                known = list(last.active_service_names) + list(last.silent_service_names)
                pipeline.service_tracker.seed(known)
        except Exception as exc:
            logger.error("[Batch run %d] Failed: %s", run_count, exc)
            output = f"[Assessment failed]: {exc}"

        print(f"\n{'='*70}")
        print(f"  BATCH ASSESSMENT  |  Run {run_count}")
        print(f"{'='*70}\n")
        print(output)
        print(f"{'='*70}\n")

        logger.info("[Batch run %d] Next run in %d minutes.", run_count, interval_minutes)
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    main()
