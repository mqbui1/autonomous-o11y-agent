# Self-Contained Deployment (customer sites)

This is the deployment path for running the agent **against a customer's existing
Splunk Observability Cloud org**, with **no AWS/Bedrock dependency** — the LLM is
our own fine-tuned 14B model, served locally via Ollama. It does **not** deploy
the Astronomy Shop demo app (that's for internal demos only — see
[deploy/README.md](../README.md)); the agent points at telemetry the customer's
own services are already sending.

```
Customer's existing services  →  (already sending telemetry)  →  Splunk Observability Cloud
                                                                          │
                                                                          ▼ API queries
                          ┌───────────────────────────────────────────────────────┐
                          │  Customer site / VM (this deployment)                  │
                          │                                                        │
                          │   Ollama (o11y-agent-14b)  ←──┐                        │
                          │        ▲                      │                       │
                          │        │ OpenAI-compat API     │ chat assistant        │
                          │   O11y Agent               Supervisor UI (:9090)       │
                          │   10 specialists            Pending Remediations       │
                          └───────────────────────────────────────────────────────┘
```

## What gets packaged

Three things ship to a customer site, all pulled from GitHub except the model
weights:

| Component | Source | Size |
|---|---|---|
| `autonomous-o11y-agent` (this repo) | `git clone` | ~few MB |
| 4 companion tool repos — `auto-detector-provisioner`, `o11y-usage-governance`, `o11y-instrumentation-analyzer`, `splunk-o11y-health-check` | `git clone` (siblings) | ~few MB each |
| `splunk-otel-supervisor` (Supervisor UI) | `git clone` (sibling) | ~few MB |
| Fine-tuned model weights (`o11y-agent-14b`, GGUF, Q4_K_M quant) | **see below** — too large for git | ~8.4 GB |

> **Open decision, not yet resolved:** the GGUF is currently only transferred by
> hand (`scp`) between machines we control. Before this becomes a repeatable
> customer deliverable, we need to decide where it's hosted for download — see
> [Distributing the model weights](#distributing-the-model-weights) below.

## Prerequisites

### Software
- Docker Engine + **Docker Compose v2** (the `docker compose` plugin, not the
  standalone `docker-compose` v1 binary — v1 doesn't support the compose-spec
  `!reset` tag or extended `depends_on` syntax this repo's compose files use).
  Verify with `docker compose version` → should print `v2.x` or higher.
- `git`

### Hardware sizing

| | Minimum (CPU-only) | Recommended |
|---|---|---|
| CPU | 4 vCPU | 8 vCPU |
| RAM | 16 GB | 30 GB+ |
| Disk | 30 GB free | 50 GB free |
| GPU | None (CPU inference works, just slower) | NVIDIA GPU + `nvidia-container-toolkit` |

RAM budget is driven by the model's KV cache, not just weights — see
[ec2-cpu-basic-test.md](ec2-cpu-basic-test.md#memory-budget-why-num_ctx-was-trimmed)
for the math. Two Modelfile variants exist for this reason:
- `training/output/Modelfile` — full `num_ctx=32768`, ~14.7 GB total. Use on
  boxes with ≥24 GB RAM.
- `training/output/Modelfile.cpu-ec2` — trimmed `num_ctx=8192` (matches the
  model's own training sequence length, so no capability is lost), ~9.9 GB
  total. Use on tighter 16 GB boxes.

### Access
- A Splunk Observability Cloud **access token** (API + INGEST scope) and
  **realm** for the org whose telemetry the agent will assess.
- The `SPLUNK_ENVIRONMENT` value matching the `deployment.environment`
  resource attribute the customer's services already report.

No AWS/Bedrock credentials are required for this deployment path.

## Where to download

### 1. The repos
```bash
mkdir -p ~/o11y-agent-deploy && cd ~/o11y-agent-deploy
git clone https://github.com/mqbui1/autonomous-o11y-agent.git
git clone https://github.com/mqbui1/auto-detector-provisioner.git
git clone https://github.com/mqbui1/o11y-usage-governance.git
git clone https://github.com/mqbui1/o11y-instrumentation-analyzer.git
git clone https://github.com/mqbui1/splunk-o11y-health-check.git
git clone https://github.com/mqbui1/splunk-otel-supervisor.git
```
All six must sit as **siblings in the same parent directory** — `deploy/docker-compose.yml`
references the tool repos and the supervisor repo by relative path (`../../<repo>`).

### 2. The model weights
```bash
mkdir -p autonomous-o11y-agent/training/models/qwen14b-o11y-agent
# TODO: download command goes here once a hosting decision is made — see below.
```

### Distributing the model weights

The GGUF is 8.4 GB — too large for a normal git push (this repo's `.gitignore`
excludes `training/models/` and `training/output/*.gguf` entirely) and too
large for a single GitHub Release asset (2 GB/file limit). Options, none yet
chosen:

| Option | Pros | Cons |
|---|---|---|
| **Hugging Face Hub** (private or public model repo) | Purpose-built for this — versioning, resumable downloads, `huggingface-cli download`; free, no practical size cap | Requires a decision on public vs. private (this model was fine-tuned on our own captured incident data) |
| **GitHub Release, split into <2 GB parts** | Stays inside GitHub, no new account/service | Manual split/`cat`-rejoin step for every customer; awkward to version |
| **Internal artifact store (S3 + presigned URLs, etc.)** | Full control over access, matches how other internal artifacts are likely already distributed | More infra to stand up and maintain; not yet built |

**This needs a decision before it's a repeatable customer deliverable** — flagging
rather than picking one, since it's effectively a decision about publishing a
model trained on internal data outside the current environment.

## Deployment

```bash
cd autonomous-o11y-agent/deploy
cp .env.example .env
# Edit .env — set at minimum:
#   SPLUNK_ACCESS_TOKEN, SPLUNK_REALM, SPLUNK_ENVIRONMENT
#   LLM_PROVIDER=ollama
#   OLLAMA_MODEL=o11y-agent-14b
#   OLLAMA_BASE_URL=http://ollama:11434/v1   # trailing /v1 required (OpenAI-compat path)
#   SPECIALIST_TIMEOUT=3600   # CPU inference is much slower than GPU; raise generously

# Build (GPU host):
docker compose --profile local-llm build ollama
docker compose --profile local-llm up -d ollama o11y-agent supervisor

# Build (CPU-only host):
docker compose -f docker-compose.yml -f docker-compose.cpu.yml --profile local-llm build ollama
docker compose -f docker-compose.yml -f docker-compose.cpu.yml --profile local-llm up -d ollama o11y-agent supervisor
```

Only three services are started — `ollama`, `o11y-agent`, `supervisor`. The
Astronomy Shop / OTel Collector / Locust services in this compose file are for
internal demo use only and are not started here.

## Verify

```bash
docker logs -f ollama       # should NOT show "could not select device driver"
docker logs -f o11y-agent   # let a full assessment run to completion
```
Then open the Supervisor UI at `http://localhost:9090` (or tunnel it — see
below) — the **Agent** tab populates after the first assessment cycle.

### Accessing the Supervisor UI remotely
If deploying to a remote box (e.g. EC2) without opening port 9090 in a
security group, tunnel it over SSH instead:
```bash
ssh -L 9090:localhost:9090 <user>@<host>
```
Then browse to `http://localhost:9090` on your local machine.
