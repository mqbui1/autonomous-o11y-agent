# EC2 t2.xlarge — basic CPU-only capability test (prep checklist)

**Scope:** not a performance/parity test — the goal is to prove the
self-contained package (agent + local 14B via Ollama) can start, load the
model without OOM, and complete at least one full specialist assessment on
commodity CPU hardware (4 vCPU / 16GB RAM / no GPU, matching the
internally-provisioned `t2.xlarge` size).

**Key scoping decision:** do NOT deploy the Astronomy Shop demo stack on this
box. The agent talks to Splunk Observability Cloud's real API (via
`SPLUNK_ACCESS_TOKEN`/`SPLUNK_REALM`/`SPLUNK_ENVIRONMENT`), not to local
microservices — this matches how a real customer would deploy it (their app
infra is elsewhere; only the agent + local LLM get deployed at the customer
site/VM). Point this box at the same org/environment already generating
telemetry (`astroshop-local`) rather than replicating the whole demo stack.
This keeps the RAM budget to just `ollama` + `o11y-agent`, which is what
actually needs validating here.

## Memory budget (why num_ctx was trimmed)

Qwen2.5-14B's GQA KV cache costs ~192KB/token in Ollama's default f16 cache
(2 × 48 layers × 8 kv_heads × 128 head_dim × 2 bytes). At the GPU-host default
`num_ctx 32768` that's ~6.3GB of KV cache on top of the ~8.4GB Q4_K_M model
weights — ~14.7GB total, leaving under 1.5GB headroom on a 16GB box for
everything else (OS, Docker, the agent container). `docker-compose.cpu.yml`
already swaps in a trimmed `num_ctx 8192` Modelfile for this reason — that
matches the QLoRA fine-tune's own training `sequence_len` (8192), so nothing
is lost, and brings the total down to ~9.9GB (~6GB headroom).

## Once the box is up

1. **Install Docker + Compose plugin** (Ubuntu):
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo apt-get install -y docker-compose-plugin
   ```

2. **Get the repo onto the box** (git clone as normal — the GGUF is
   git-ignored, see step 3):
   ```bash
   git clone <repo-url> autonomous-o11y-agent
   ```

3. **Transfer the 8.4GB GGUF separately** (not in git):
   ```bash
   scp training/models/qwen14b-o11y-agent/qwen14b-o11y-agent-q4_k_m.gguf \
     <ec2-host>:autonomous-o11y-agent/training/models/qwen14b-o11y-agent/
   ```

4. **Set env vars** (`.env` in `deploy/`):
   ```
   SPLUNK_ACCESS_TOKEN=<same token as astroshop-local>
   SPLUNK_REALM=<realm>
   SPLUNK_ENVIRONMENT=astroshop-local
   LLM_PROVIDER=ollama
   OLLAMA_MODEL=o11y-agent-14b
   OLLAMA_BASE_URL=http://ollama:11434/v1
   SPECIALIST_TIMEOUT=3600
   ```
   `OLLAMA_BASE_URL` needs the trailing `/v1` — the agent talks to Ollama over
   its OpenAI-compat endpoint (`config.py`'s default already includes it;
   this only matters if you override the value). `SPECIALIST_TIMEOUT` bumped
   well above the 900s default — CPU-only
   inference on 4 vCPUs will be much slower per turn than the GPU host this
   was validated on. This is a basic capability check, not a speed test; let
   it take however long it takes.

5. **Build and start just `ollama` + `o11y-agent`** (skip the full compose
   file's other services entirely — no Astronomy Shop needed here):
   ```bash
   cd deploy
   docker compose -f docker-compose.yml -f docker-compose.cpu.yml \
     --profile local-llm build ollama
   docker compose -f docker-compose.yml -f docker-compose.cpu.yml \
     --profile local-llm up -d ollama o11y-agent
   ```

6. **Watch for the real success/failure signals**:
   ```bash
   docker logs -f ollama       # should NOT show "could not select device driver"
   docker logs -f o11y-agent   # let a full assessment run to completion
   ```
   Success = a full assessment completes with real per-domain findings (no
   crash, no OOM-kill, no "max turns without completing"). Compare against
   the same environment's Bedrock output qualitatively — exact latency parity
   is explicitly out of scope for this pass.

## What NOT to worry about for this test
- Wall-clock speed (CPU inference will be much slower than the GPU-backed
  parity suite — that's expected and not a failure).
- Running the full 10-specialist run concurrently — Ollama serializes
  requests through a single CPU-bound model slot regardless of
  `specialist_max_concurrency`, so specialists will queue, not parallelize.
  Let it run unattended; there's no code change needed to handle this for a
  one-off basic test.
