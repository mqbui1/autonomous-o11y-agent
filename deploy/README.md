# Astronomy Shop + Splunk + Autonomous O11y Agent

Local docker-compose stack that runs the OpenTelemetry Astronomy Shop, a Splunk gateway
OTel collector, and the Autonomous O11y Agent — all wired together.

```
Astronomy Shop (17 services)
        │ OTLP traces/metrics/logs
        ▼
Splunk Gateway OTel Collector   ──► Splunk APM (traces)
        │                       ──► Splunk IM  (metrics + histograms)
        │                       ──► Splunk Log Observer (logs)
        ▼
Autonomous O11y Agent  ──► queries Splunk APIs every 30 min
                       ──► 9 specialist agents: health, instrumentation,
                           governance, detector, logs, RUM, RCA, synthetics, DB
```

## Prerequisites

- Docker Desktop (Mac/Linux) with ≥8 GB RAM allocated
- Splunk Observability Cloud account with:
  - API access token (ingest + API scopes)
  - HEC token + endpoint
- AWS credentials with Bedrock access (Claude Sonnet 4.6) **or** an OpenAI-compatible API key

## Quick start

```bash
# 1. Copy and fill in credentials
cp .env.example .env
# Edit .env — at minimum: SPLUNK_ACCESS_TOKEN, SPLUNK_REALM, SPLUNK_ENVIRONMENT,
#             SPLUNK_HEC_TOKEN, SPLUNK_HEC_URL, AWS_* (or OPENAI_*)

# 2. Start everything
docker compose up -d

# 3. Watch services come up (takes ~2 min for all images to pull)
docker compose ps

# 4. Open the Astronomy Shop
open http://localhost:8080

# 5. Watch the O11y Agent run assessments
docker compose logs -f o11y-agent
```

## Ports

| Service | URL |
|---------|-----|
| Astronomy Shop | http://localhost:8080 |
| Load Generator (Locust) | http://localhost:8089 |
| OTel Collector health | http://localhost:13133 |

## Common commands

```bash
# Stop everything
docker compose down

# Stop and wipe state
docker compose down -v

# Restart just the agent (e.g. after a config change)
docker compose restart o11y-agent

# Run a one-shot assessment right now
docker compose exec o11y-agent python3 main.py

# Rebuild the agent image after code changes
docker compose build o11y-agent && docker compose up -d o11y-agent

# Check collector is exporting correctly
curl -s http://localhost:13133/ | python3 -m json.tool
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SPLUNK_ACCESS_TOKEN` | yes | API + ingest token |
| `SPLUNK_REALM` | yes | e.g. `us1`, `eu0` |
| `SPLUNK_ENVIRONMENT` | yes | e.g. `astronomy-shop-local` |
| `SPLUNK_HEC_TOKEN` | yes | For log forwarding |
| `SPLUNK_HEC_URL` | yes | HEC endpoint URL |
| `AWS_ACCESS_KEY_ID` | if using Bedrock | |
| `AWS_SECRET_ACCESS_KEY` | if using Bedrock | |
| `LLM_PROVIDER` | no | `bedrock` (default) or `openai` |
| `OPENAI_BASE_URL` | if `LLM_PROVIDER=openai` | |
| `WATCH_INTERVAL` | no | Assessment interval in minutes (default: 30) |
| `AUTO_APPLY` | no | `true` to auto-apply detector recommendations |
| `LOCUST_USERS` | no | Concurrent load generator users (default: 5) |

## Memory requirements

Total approximate footprint with all services running:

| Component | RAM |
|-----------|-----|
| Astronomy Shop (17 services) | ~2.5 GB |
| Kafka | 600 MB |
| Splunk OTel Collector | 256 MB |
| Autonomous O11y Agent | 512 MB |
| Jaeger + Grafana (stubs) | 300 MB |
| **Total** | **~4.2 GB** |

Docker Desktop default is 8 GB — recommended to leave it at 8 GB.

## What you'll see in Splunk

After ~5 minutes of traffic:

- **APM** → 17 instrumented services in the service map
- **Infrastructure Monitoring** → custom dashboards, metrics from all services
- **Log Observer** → structured logs from all services (index: `astronomyshop`)
- **O11y Agent output** → printed to `docker compose logs o11y-agent`, includes full
  cross-domain assessment across health, instrumentation, governance, detectors, logs,
  RCA, synthetics, and DB domains
