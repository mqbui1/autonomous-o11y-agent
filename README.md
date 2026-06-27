# Autonomous O11y Agent

Autonomous observability agent for Splunk Observability Cloud. Orchestrates four specialized tools — detector provisioning, cardinality governance, instrumentation analysis, and health auditing — into a single continuous loop powered by Claude via AWS Bedrock.

## Architecture

```
autonomous-o11y-agent/
  main.py         — CLI entrypoint
  agent.py        — Strands agent + tool registration + system prompt
  config.py       — AgentConfig dataclass
  narrator.py     — Bedrock Claude narrative synthesis
  loop.py         — continuous watch loop
  tools/
    provisioner.py  — wraps auto-detector-provisioner
    governance.py   — wraps o11y-usage-governance
    analyzer.py     — wraps o11y-instrumentation-analyzer
    health_check.py — wraps splunk-o11y-health-check
```

Each tool module wraps an existing project as a subprocess. The Strands agent orchestrates them, reasons about the output, and decides what to investigate or apply next.

## Prerequisites

The four tool projects must be cloned as siblings to this repo (or their paths configured via env vars):

```
Documents/
  autonomous-o11y-agent/     ← this repo
  auto-detector-provisioner/ ← https://github.com/mqbui1/auto-detector-provisioner
  o11y-usage-governance/     ← https://github.com/mqbui1/o11y-usage-governance
  o11y-instrumentation-analyzer/ ← https://github.com/mqbui1/o11y-instrumentation-analyzer
  splunk-o11y-health-check/  ← https://github.com/mqbui1/splunk-o11y-health-check
```

Install each project's dependencies separately:

```bash
pip install -r ../auto-detector-provisioner/requirements.txt
pip install -r ../o11y-usage-governance/requirements.txt
pip install -r ../o11y-instrumentation-analyzer/requirements.txt
pip install -r ../splunk-o11y-health-check/requirements-health-hub.txt
```

## Setup

```bash
cd autonomous-o11y-agent
pip install -e .

cp .env.example .env
# Edit .env with your Splunk realm, token, and AWS credentials
```

## Usage

```bash
# One-shot full assessment (dry-run — no changes made)
python3 main.py --realm us1 --token $TOKEN --environment production

# One-shot with auto-apply (applies cardinality fixes, deploys detectors)
python3 main.py --realm us1 --token $TOKEN --environment production --auto-apply

# Scope to a specific service
python3 main.py --realm us1 --token $TOKEN --environment production --service payment-service

# Ask the agent a specific question
python3 main.py --realm us1 --token $TOKEN --environment production \
  --prompt "Which services have the worst instrumentation coverage and why?"

# Continuous watch mode — runs every 60 minutes
python3 main.py --realm us1 --token $TOKEN --environment production --watch

# Watch mode with custom interval and auto-apply
python3 main.py --realm us1 --token $TOKEN --environment production \
  --watch --interval 30 --auto-apply

# Skip narrative synthesis (raw agent output only, faster)
python3 main.py --realm us1 --token $TOKEN --environment production --no-narrative
```

## What the agent does

On each run, the agent follows this sequence:

1. **Health audit** — `check_detector_health` + `check_apm_health` to understand current state
2. **Instrumentation analysis** — `analyze_instrumentation` to find span/metric/log gaps
3. **Cardinality governance** — `scan_cardinality` + `scan_cardinality_anomalies` to find waste
4. **Detector provisioning** — `provision_detectors` dry-run to identify coverage gaps
5. **Narrative synthesis** — Claude synthesizes all findings into a prioritized summary

In `--auto-apply` mode, the agent will:
- Deploy missing detectors tuned to actual baselines
- Retune existing detectors when baselines have drifted
- Flag cardinality fixes (YAML generated but not auto-applied — requires collector config change)

## Environment variables

| Variable | Description |
|----------|-------------|
| `SPLUNK_REALM` | Splunk Observability realm (e.g. `us1`) |
| `SPLUNK_ACCESS_TOKEN` | API access token |
| `SPLUNK_ENVIRONMENT` | Target environment name |
| `AWS_DEFAULT_REGION` | AWS region for Bedrock (default: `us-west-2`) |
| `AWS_ACCESS_KEY_ID` | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials |
| `PROVISIONER_PATH` | Override path to auto-detector-provisioner |
| `GOVERNANCE_PATH` | Override path to o11y-usage-governance |
| `ANALYZER_PATH` | Override path to o11y-instrumentation-analyzer |
| `HEALTH_CHECK_PATH` | Override path to splunk-o11y-health-check |
| `TOOL_TIMEOUT` | Subprocess timeout per tool call in seconds (default: `300`) |
