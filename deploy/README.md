# Astronomy Shop + Splunk + Autonomous O11y Agent

```
Astronomy Shop (17 services)  →  Splunk Gateway OTel Collector  →  Splunk O11y Cloud
                                          │
                                          ▼ fan-out (OTLP/HTTP)
                                  Autonomous O11y Agent
                                  9 specialists · 30-min cycle
                                          │
                                          ▼ REST API
                                  Supervisor UI (http://localhost:9090)
```

## Quick start

```bash
cp .env.example .env          # fill in SPLUNK_ACCESS_TOKEN, SPLUNK_INGEST_TOKEN,
                              # SPLUNK_REALM, SPLUNK_ENVIRONMENT, AWS_* (or OPENAI_*)
docker compose up -d
```

First run pulls ~3 GB of images. All services healthy in ~2 minutes.

## URLs

| | URL |
|---|---|
| Astronomy Shop | http://localhost:8080 |
| **Supervisor UI** (Agent findings) | **http://localhost:9090** |
| Locust load generator | http://localhost:8089 |
| OTel Collector health | http://localhost:13133 |

The **Agent** tab in the Supervisor UI populates after the first 30-minute assessment cycle.
Trigger one immediately: `docker compose up -d o11y-agent` (agent runs on startup).

## Docs

- [Configuration reference](docs/configuration.md) — all `.env` variables
- [Supervisor integration](docs/supervisor-integration.md) — how the UI connects to the agent
- [Operations guide](docs/operations.md) — common commands, AWS token refresh, troubleshooting
