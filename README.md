# Autonomous O11y Agent

Runs ten specialist AI agents in parallel against Splunk Observability Cloud, then synthesizes their findings into a prioritized assessment with automated remediations. Supports AWS Bedrock and any OpenAI-compatible LLM.

## Architecture

```mermaid
flowchart LR
    COLL["OTel Collector"]

    subgraph Splunk["Splunk Observability Cloud"]
        APM["APM · Profiling"]
        IM["Infra Metrics"]
        RUM["RUM · Synthetics"]
        LOGS["Logs · Detectors"]
    end

    subgraph Agent["Autonomous O11y Agent"]
        COORD["Coordinator"]
        SPECS["10 Specialists\n─────────────────\n⚡ Performance\n❤️  Health\n🔬 Instrumentation\n⚖️  Governance\n🚨 Detector\n📋 Logs\n🌐 RUM\n🔍 RCA\n🤖 Synthetics\n🗄️  DB / Dependency"]
        REM["Remediation Engine"]
        STATE["State Store"]
        COORD --> SPECS --> REM --> STATE
    end

    subgraph UI["Supervisor UI"]
        DASH["Assessment Dashboard"]
        ACTIONS["ActionEngine\ncreate_detector · restart_service\nadd_db_instrumentation · code_fix"]
    end

    COLL -->|traces · metrics\nprofiling logs| Splunk
    Splunk -->|API queries| COORD
    STATE -->|findings + remediations| DASH
    DASH -->|approve & apply| ACTIONS
    ACTIONS -->|updates| Splunk
```

## Quick start

```bash
pip install -e .

# One-shot assessment
python3 main.py --realm us1 --token $TOKEN --environment production

# Watch mode (runs every 60 min)
python3 main.py --realm us1 --token $TOKEN --environment production --watch

# Streaming mode (always-on OTLP receiver + batch assessments)
python3 main.py --realm us1 --token $TOKEN --environment production --streaming

# Ask a specific question
python3 main.py --realm us1 --token $TOKEN --environment production \
  --prompt "Which services have the worst instrumentation coverage?"
```

## Docker Compose (local demo stack)

Runs the full stack locally: Astronomy Shop → Splunk OTel Collector → O11y Agent → Supervisor UI.

```bash
cd deploy
cp .env.example .env   # fill in credentials
docker compose up -d
```

| | URL |
|---|---|
| Astronomy Shop | http://localhost:8080 |
| Supervisor UI (agent findings) | http://localhost:9090 |
| Locust load generator | http://localhost:8089 |

See [deploy/README.md](deploy/README.md) for full setup instructions.

## Specialists

| Specialist | What it assesses |
|---|---|
| **Health** | Detector coverage, APM error/latency, OTel Collector pipeline health, license utilization |
| **Instrumentation** | Span quality, missing attributes, signal coverage per service, overall score (0–100) |
| **Governance** | Metric cardinality explosions, slow-burn MTS growth, trace volume anomalies |
| **Detector** | Dark services, behavioral baselines, detector provisioning and retuning |
| **Logs** | Error log patterns, volume anomalies, services with zero log output |
| **RUM** | Frontend JS errors, Core Web Vitals (LCP, FID, CLS), unconfigured RUM apps |
| **RCA** | Active incident investigation, causal chain analysis, change correlation |
| **Synthetics** | Test coverage gaps, failing tests, degrading performance trends |
| **DB/Dependency** | Inferred service topology, slow outbound calls, `db.*` attribute coverage |
| **Performance** | AlwaysOn Profiling hotspots, N+1 query patterns, latency outliers, code-level fix generation |

### Performance specialist tiers

The performance specialist degrades gracefully based on available data:

| Tier | Requires | Output |
|---|---|---|
| **A** | AlwaysOn Profiling + source code | Exact file:line diff ready to apply |
| **B** | AlwaysOn Profiling only | file:line:function with precise fix description |
| **C** | Span patterns only (always available) | Operation-level N+1 / latency fix recommendation |
| **D** | Nothing detectable | Skipped — no findings emitted |

Configure source code access via `SOURCE_ROOT` (local path) or `GITHUB_TOKEN` + `GITHUB_REPO`.

## Deployment modes

| Mode | Command flag | Best for |
|---|---|---|
| Batch | _(default)_ | Periodic deep assessments |
| Watch | `--watch` | Continuous scheduled assessment loop |
| Streaming | `--streaming` | Always-on OTLP receiver + real-time detection (PII, cardinality, new services) |

## LLM providers

```bash
# AWS Bedrock (default)
AWS_DEFAULT_REGION=us-west-2 python3 main.py ...

# Any OpenAI-compatible endpoint (Luna, Azure, Ollama)
LLM_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_MODEL=llama3.1 python3 main.py ...
```

## Tool dependencies

The agent calls four companion CLI tools. Clone them as siblings to this repo:

```
auto-detector-provisioner/
o11y-usage-governance/
o11y-instrumentation-analyzer/
splunk-o11y-health-check/
```

The agent degrades gracefully if any tool is missing — specialists that depend on it report the gap rather than crashing the full run.

## Remediation

After all specialists complete, the coordinator runs a rule-based remediation pass (`agents/remediation.py`) that maps critical and high-severity issues to actionable supervisor operations. The results are saved as `pending_remediations` in the assessment JSON.

Each remediation includes:
- `action_type` — the supervisor ActionEngine operation to run (e.g. `create_splunk_detector`, `reload_collector`, `restart_service`)
- `action_payload` — ready-to-execute arguments
- `auto_applicable` — `true` if the action can run without manual config edits

The Supervisor UI surfaces these as a **Pending Remediations** panel with per-item Apply buttons and a bulk "Apply Selected" option. Chat also accepts natural-language remediation requests ("apply the detector fix for checkout-service").

| Issue pattern | Action | Auto? |
|---|---|---|
| No detector / dark service (per-service) | `create_splunk_detector` (error_rate + latency) | ✅ |
| No detector coverage (org-wide) | `build_detectors` | ✅ |
| OTel Collector unreachable | `reload_collector` | ✅ |
| DB instrumentation missing (`db.system` absent) | `add_db_instrumentation` | ✅ |
| DB span attributes stripped by collector processor | `patch_collector_config` | ⬜ |
| Silent service / no telemetry | `restart_service` | ✅ |
| Detector threshold too tight / noisy | `rebaseline_detectors` | ✅ |
| Performance hotspot with profiling data | `generate_code_fix` (file:line diff) | ⬜ |

## Supervisor UI integration

The [Splunk OTel Supervisor](https://github.com/mqbui1/splunk-otel-supervisor) renders assessment findings in a web dashboard. After each run the agent exposes `GET /api/assessment/latest` and the supervisor proxies it to its **Agent** tab. The **Pending Remediations** panel lets operators apply fixes with or without human approval. See [deploy/docs/supervisor-integration.md](deploy/docs/supervisor-integration.md).
