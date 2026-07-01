# Operations Guide

## Common commands

```bash
# Start everything
docker compose up -d

# Stop everything (state is preserved in the agent-state volume)
docker compose down

# Stop and wipe all state including assessment history
docker compose down -v

# Trigger an assessment immediately
docker compose up -d o11y-agent    # agent runs on startup, then waits WATCH_INTERVAL

# Watch agent logs
docker compose logs -f o11y-agent

# Fetch the latest assessment as JSON
curl -s http://localhost:4319/api/assessment/latest | python3 -m json.tool

# Rebuild agent after code changes
docker compose build o11y-agent && docker compose up -d o11y-agent

# Rebuild supervisor UI after frontend changes
docker compose build supervisor && docker compose up -d supervisor
```

## AWS session token refresh

Bedrock session tokens expire after ~1 hour. When the agent logs show:

```
ExpiredTokenException: The security token included in the request is expired
```

Run from the `deploy/` directory:

```bash
./refresh-aws-creds.sh
```

This fetches fresh credentials from your local AWS profile (`aws configure export-credentials`),
updates `.env` in-place, and recreates the agent container to pick them up.

## Troubleshooting

**Astronomy Shop images fail to pull (403 Forbidden from ghcr.io)**
```bash
docker logout ghcr.io
docker compose up -d
```
Stale GitHub credentials in Docker's credential store block anonymous pulls of public images.

**Agent tab shows "No assessment yet"**
- The first assessment runs on agent startup. Check `docker compose logs o11y-agent` —
  you should see `[Batch run 1] Starting assessment` within 15 seconds.
- If the agent errors immediately, check AWS credentials (`AWS_SESSION_TOKEN` may need refresh).

**Collector shows no data in Splunk**
- Verify `SPLUNK_INGEST_TOKEN` is an ingest-scope token (not API-only).
- Check: `curl -s http://localhost:13133/` — should return `{"status":"Server available"}`.
- Check: `docker compose logs otel-collector | grep -i error`.

**Memory: containers OOM-killed**
- Docker Desktop needs ≥12 GB. Check Settings → Resources → Memory.
- Kafka is the largest single consumer (~600 MB JVM). If it keeps restarting, increase memory.
