# Training: Fine-tune a local O11y Agent

Replaces AWS Bedrock with a locally-hosted model (qwen2.5:14b or similar) fine-tuned on real assessment conversations from your Splunk Observability Cloud environment.

## Workflow

```
Real assessments  →  generate_from_assessments.py  →  data/train.jsonl  →  finetune.py  →  Ollama
```

## Step 1 — Collect training data

### From existing assessments (immediate)

Convert `*_detail.json` files already saved by the agent:

```bash
python3 training/generate_from_assessments.py
# → training/data/train.jsonl
```

These produce instruction-following examples:
- One per specialist (system prompt + task → full assessment text)
- One synthesis example (specialist summaries → final prioritized report)

### From live conversations (ongoing)

Set `CAPTURE_TRAINING_DATA=true` in `.env` (already set) and run assessments normally.
Each completed agent loop is saved to `/root/.o11y-agent/training/*.jsonl`.
These full multi-turn conversations (including tool calls) make the richest training data.

Re-run `generate_from_assessments.py` after accumulating more conversations — it picks up both sources automatically.

## Step 2 — Fine-tune

Requires a machine with a GPU (24 GB VRAM for 14B in 4-bit):

```bash
pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git" trl datasets

python3 training/finetune.py
# defaults: Qwen/Qwen2.5-14B-Instruct, 3 epochs, LR 2e-4
```

Options:
```
--model   HuggingFace model ID   (default: Qwen/Qwen2.5-14B-Instruct)
--epochs  Number of epochs       (default: 3)
--batch   Per-device batch size  (default: 2)
--lr      Learning rate          (default: 2e-4)
--output  Output directory       (default: training/output/)
```

Outputs:
- `training/output/lora-adapter/` — LoRA weights (small, ~200 MB)
- `training/output/merged/` — full merged model (optional)
- `training/output/o11y-agent.gguf` — GGUF for Ollama (optional, prompted at end)

## Step 3 — Load into Ollama

After exporting the GGUF:

```
# training/output/Modelfile
FROM ./o11y-agent.gguf
PARAMETER temperature 0.3
PARAMETER num_ctx 8192
SYSTEM "You are an autonomous observability specialist for Splunk Observability Cloud."
```

```bash
ollama create o11y-agent -f training/output/Modelfile
```

Update `.env`:
```env
OPENAI_MODEL=o11y-agent
```

Recreate the agent container to pick up the new model:
```bash
cd deploy && docker compose up -d o11y-agent
```

## Data volume guidelines

| Training examples | Expected quality |
|---|---|
| < 50 | Too few — model won't improve meaningfully |
| 50–200 | Good for instruction style alignment |
| 200–500 | Solid tool-calling behavior |
| 500+ | Strong domain adaptation |

Each 30-min assessment cycle with `CAPTURE_TRAINING_DATA=true` produces ~10–20 examples.
