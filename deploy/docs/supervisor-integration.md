# Supervisor Integration

The [Splunk OTel Supervisor](https://github.com/mqbui1/splunk-otel-supervisor) is a companion
web UI that surfaces the agent's structured findings in a purpose-built dashboard.

## Architecture

```
o11y-agent container (:4318)
  └── Flask server
        ├── POST /v1/traces          ← OTLP fan-out from gateway collector
        ├── GET  /api/assessment/latest   → full latest assessment JSON
        └── GET  /api/assessment/history  → last 20 run summaries

supervisor container (:8080 → host :9090)
  └── FastAPI + static UI
        ├── GET /api/assessment/latest   → proxied from o11y-agent
        ├── GET /api/assessment/history  → proxied from o11y-agent
        ├── GET /api/assessment/status   → agent reachability check
        └── GET /                        → browser UI
```

Both containers share the `opentelemetry-demo` Docker bridge network. The supervisor
reaches the agent at `http://o11y-agent:4318` (container name as hostname).

## Data flow

1. After each batch assessment, the agent writes `~/.o11y-agent/{environment}_detail.json`
   containing all 9 specialist findings, cross-domain analysis, and the synthesis narrative.
2. The agent's Flask server reads this file on every `GET /api/assessment/latest` request.
3. The supervisor's `agent_bridge.py` calls that endpoint and returns the result to the browser.
4. `app.js` renders the response as a card-per-specialist grid with severity-coded borders,
   issue lists, a cross-domain panel, the synthesis text, and a run history table.

## What the Agent tab shows

| Section | Source |
|---------|--------|
| 9 specialist cards | `specialists.*` — one per domain (health, instrumentation, governance, detector, logs, RUM, RCA, synthetics, DB) |
| Card border color | Worst issue severity across that specialist's findings |
| Issues list | `specialists.*.issues` — severity, service, description, recommendation |
| Cross-domain panel | `cross_domain` — services flagged by multiple specialists |
| Synthesis | `synthesis` — full LLM narrative: executive summary, action plan, health snapshot |
| Run history | `/api/assessment/history` — last 20 runs with score and service counts |

## Loose coupling

The supervisor has no dependency on the agent's codebase. It only needs two HTTP endpoints
and a shared Docker network. The integration can be configured via one env var:

```env
O11Y_AGENT_URL=http://o11y-agent:4318   # default; change for remote deployments
```
