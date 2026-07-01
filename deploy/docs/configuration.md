# Configuration Reference

Copy `.env.example` to `.env` and fill in the values below.

## Required

| Variable | Description |
|----------|-------------|
| `SPLUNK_ACCESS_TOKEN` | API-scope token — used by the agent to query Splunk APIs |
| `SPLUNK_INGEST_TOKEN` | Ingest-scope token — used by the OTel collector to export telemetry |
| `SPLUNK_REALM` | Your Splunk O11y realm, e.g. `us1`, `eu0` |
| `SPLUNK_ENVIRONMENT` | Environment label for all telemetry, e.g. `astronomy-shop-local` |

## LLM provider — choose one

### AWS Bedrock (default)

| Variable | Description |
|----------|-------------|
| `AWS_ACCESS_KEY_ID` | IAM key ID |
| `AWS_SECRET_ACCESS_KEY` | IAM secret |
| `AWS_SESSION_TOKEN` | STS session token (required for temporary credentials) |
| `AWS_DEFAULT_REGION` | Bedrock region (default: `us-west-2`) |

Session tokens expire in ~1 hour. Run `./refresh-aws-creds.sh` to fetch a new one from your
local AWS profile and recreate the agent container automatically.

### OpenAI-compatible

```env
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
```

Works with Azure OpenAI, Ollama, and any other OpenAI-compatible endpoint.

## Optional tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCH_INTERVAL` | `30` | Assessment interval in minutes |
| `AUTO_APPLY` | `false` | Auto-apply safe detector recommendations |
| `LOCUST_USERS` | `5` | Concurrent load generator users |
| `DEMO_VERSION` | `latest` | Astronomy Shop image tag |
| `ALERT_WEBHOOK_URL` | — | Slack/webhook URL for streaming alerts |
| `ALERT_SUPPRESS` | — | Suppress specific alerts, e.g. `attribute:load-generator` |
