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
    from narrator import generate_narrative
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

    if args.watch:
        watch(
            agent=agent,
            config=config,
            interval_minutes=args.interval,
        )
    else:
        output = run_once(agent, config, prompt=args.prompt)

        if args.no_narrative:
            print(output)
        else:
            print("\n" + "="*70)
            print("  AUTONOMOUS O11Y AGENT ASSESSMENT")
            print("="*70 + "\n")
            print(output)
            print("\n" + "="*70)
            print("  SUMMARY")
            print("="*70 + "\n")
            narrative = generate_narrative(output, config)
            print(narrative)


if __name__ == "__main__":
    main()
