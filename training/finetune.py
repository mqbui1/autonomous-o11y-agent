"""
Fine-tune a local model on o11y agent training data.

Two backends:
  --mlx    Apple Silicon (M1/M2/M3/M4) — uses mlx-lm, Metal GPU, no CUDA needed.
           Default model: mlx-community/Qwen2.5-3B-Instruct-4bit (~2 GB download)
           pip install mlx-lm

  (default) CUDA GPU — uses Unsloth 4-bit LoRA, requires 20+ GB VRAM for 14B.
           Default model: Qwen/Qwen2.5-14B-Instruct
           pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git" trl datasets

Input:   training/data/synthetic.jsonl   (from generate_synthetic.py)
         training/data/train.jsonl        (real runs, optional)
Output:  training/output/

Usage (Mac):
    python3 training/generate_synthetic.py --count 1200
    python3 training/finetune.py --mlx
    python3 training/finetune.py --mlx --iters 1500 --batch 8

Usage (CUDA):
    python3 training/finetune.py
    python3 training/finetune.py --model Qwen/Qwen2.5-14B-Instruct --epochs 3
"""

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys

# HuggingFace Hub downloads fail with SSL cert verification on Splunk corporate network.
# Disabling here allows the model snapshot download to proceed.
os.environ.setdefault("HF_HUB_DISABLE_SSL_VERIFICATION", "1")

DATA_DIR    = pathlib.Path(__file__).parent / "data"
OUTPUT_DIR  = pathlib.Path(__file__).parent / "output"

# MLX defaults (Mac Apple Silicon)
# Local path takes priority if the model was downloaded via curl (avoids corporate SSL issues).
_LOCAL_MODEL_DIR = pathlib.Path(__file__).parent / "models" / "qwen2.5-3b-mlx"
MLX_MODEL = str(_LOCAL_MODEL_DIR) if _LOCAL_MODEL_DIR.exists() and (_LOCAL_MODEL_DIR / "config.json").exists() \
            else "mlx-community/Qwen2.5-3B-Instruct-4bit"
MLX_LORA_LAYERS = 16   # top-N transformer layers to fine-tune
MLX_BATCH       = 2     # smaller batches reduce memory pressure and gradient variance
MLX_ITERS       = 1800  # ~3 epochs over 1200 examples at batch 2
MLX_LR          = 5e-6  # very conservative — higher rates cause NaN divergence
MLX_MAX_SEQ_LEN = 2048  # 4096 pushes memory to 30 GB on 24 GB M4 Pro

# Unsloth/CUDA defaults
CUDA_MODEL     = "Qwen/Qwen2.5-14B-Instruct"
MAX_SEQ_LEN    = 4096
LORA_RANK      = 32


# ── Data preparation ──────────────────────────────────────────────────────────

def _load_all_examples() -> list[dict]:
    """Load synthetic + real training data, deduplicate by content hash."""
    rows = []
    for fname in ("synthetic.jsonl", "train.jsonl"):
        p = DATA_DIR / fname
        if p.exists():
            with p.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            print(f"  Loaded {fname}: added rows (total so far: {len(rows)})")
    if not rows:
        sys.exit(
            f"No training data found in {DATA_DIR}.\n"
            "Run: python3 training/generate_synthetic.py --count 1200"
        )
    return rows


def _estimate_tokens(row: dict) -> int:
    """Rough token estimate: 1 token ≈ 3.5 chars for English/code mix."""
    text = " ".join(m["content"] for m in row.get("messages", []) if m.get("content"))
    return len(text) // 3


def prepare_mlx_data(output_dir: pathlib.Path) -> pathlib.Path:
    """Split data into train/valid JSONL files for mlx-lm.
    Pre-filters examples that would exceed MLX_MAX_SEQ_LEN to prevent NaN loss.
    """
    rows = _load_all_examples()
    max_chars = MLX_MAX_SEQ_LEN * 3   # conservative char limit before tokenization
    before = len(rows)
    rows = [r for r in rows if _estimate_tokens(r) < MLX_MAX_SEQ_LEN]
    filtered = before - len(rows)
    if filtered:
        print(f"  Filtered {filtered} examples exceeding ~{MLX_MAX_SEQ_LEN} tokens")

    random.seed(42)
    random.shuffle(rows)
    split = max(1, int(len(rows) * 0.9))
    train_rows, valid_rows = rows[:split], rows[split:]

    mlx_data = output_dir / "mlx-data"
    mlx_data.mkdir(parents=True, exist_ok=True)

    for fname, subset in [("train.jsonl", train_rows), ("valid.jsonl", valid_rows)]:
        with (mlx_data / fname).open("w") as f:
            for row in subset:
                f.write(json.dumps(row) + "\n")

    print(f"MLX data prepared: {len(train_rows)} train, {len(valid_rows)} valid → {mlx_data}")
    return mlx_data


# ── MLX (Apple Silicon) fine-tune path ───────────────────────────────────────

def _ensure_model_cached(model_id: str) -> None:
    """
    Pre-download the HuggingFace model to local cache.
    Uses set_client_factory to bypass SSL cert verification on corporate networks
    (Splunk proxy presents its own cert which is not in Python's default bundle).
    """
    try:
        import httpx
        import huggingface_hub
        from huggingface_hub import snapshot_download

        # Check if already cached
        try:
            snapshot_download(model_id, local_files_only=True)
            print(f"Model already cached: {model_id}")
            return
        except Exception:
            pass

        print(f"Downloading {model_id} to HuggingFace cache...")
        huggingface_hub.set_client_factory(
            lambda: httpx.Client(verify=False, timeout=600)
        )
        path = snapshot_download(model_id)
        print(f"Model cached at: {path}")
    except Exception as e:
        print(f"WARNING: Pre-download failed ({e}). mlx-lm will attempt download directly.")


def run_mlx(args):
    try:
        import mlx_lm  # noqa: F401
    except ImportError:
        sys.exit(
            "mlx-lm not installed. Run:\n"
            "  pip install mlx-lm\n"
            "Then re-run this script."
        )

    model = args.model or MLX_MODEL
    _ensure_model_cached(model)
    output_dir = pathlib.Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    mlx_data   = prepare_mlx_data(output_dir)
    adapter_dir = output_dir / "adapters"
    fused_dir   = output_dir / "fused-3b"

    # ----- LoRA fine-tune -----
    print(f"\nFine-tuning {model} with MLX LoRA ({args.iters} steps)...")
    train_cmd = [
        sys.executable, "-m", "mlx_lm", "lora",
        "--model", model,
        "--train",
        "--data", str(mlx_data),
        "--iters", str(args.iters),
        "--batch-size", str(args.batch),
        "--num-layers", str(MLX_LORA_LAYERS),
        "--adapter-path", str(adapter_dir),
        "--learning-rate", str(args.lr),
        "--max-seq-length", str(MLX_MAX_SEQ_LEN),
        "--optimizer", "adamw",    # weight decay prevents gradient explosion
        "--mask-prompt",           # only compute loss on assistant responses
        "--val-batches", "20",
        "--steps-per-report", "10",
        "--steps-per-eval", "200",
        "--save-every", "200",
    ]
    result = subprocess.run(train_cmd)
    if result.returncode != 0:
        sys.exit("MLX training failed.")

    # ----- Fuse adapter into full model -----
    print(f"\nFusing adapter → {fused_dir} ...")
    fuse_cmd = [
        sys.executable, "-m", "mlx_lm", "fuse",
        "--model", model,
        "--adapter-path", str(adapter_dir),
        "--save-path", str(fused_dir),
        "--dequantize",  # fuse outputs bf16 weights for GGUF conversion
    ]
    result = subprocess.run(fuse_cmd)
    if result.returncode != 0:
        print("WARNING: Fuse step failed. Adapter is still usable with mlx_lm.generate.")

    # ----- Instructions for GGUF + Ollama -----
    print("\n" + "=" * 60)
    print("Fine-tune complete!")
    print(f"  Adapter:     {adapter_dir}")
    print(f"  Fused model: {fused_dir}")
    print()
    print("To test with mlx-lm directly:")
    print(f"  python -m mlx_lm.generate --model {fused_dir} \\")
    print( "      --prompt 'Assess the health of payment service...'")
    print()
    print("To convert to GGUF and load in Ollama:")
    print("  git clone https://github.com/ggerganov/llama.cpp /tmp/llama.cpp")
    print("  cd /tmp/llama.cpp && pip install -r requirements.txt")
    print(f"  python convert_hf_to_gguf.py {fused_dir} \\")
    print(f"      --outtype q4_k_m --outfile {output_dir}/o11y-agent-3b.gguf")
    print()
    print("  # Create Ollama model:")
    print(f"  echo 'FROM {output_dir}/o11y-agent-3b.gguf' > {output_dir}/Modelfile")
    print(f"  ollama create o11y-agent -f {output_dir}/Modelfile")
    print()
    print("  # Run agent with fine-tuned model:")
    print("  LLM_PROVIDER=ollama OLLAMA_MODEL=o11y-agent python3 main.py --environment <env>")
    print("=" * 60)


# ── Unsloth/CUDA fine-tune path ───────────────────────────────────────────────

def run_cuda(args):
    try:
        from unsloth import FastLanguageModel
        from trl import SFTTrainer, SFTConfig
        from datasets import Dataset
    except ImportError:
        sys.exit(
            "Unsloth / TRL not installed.\n"
            "pip install 'unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git' trl datasets"
        )

    model_id   = args.model or CUDA_MODEL
    output_dir = pathlib.Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_all_examples()
    dataset = Dataset.from_list(rows)

    print(f"Loading {model_id} (4-bit)...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_id,
        max_seq_length=MAX_SEQ_LEN,
        load_in_4bit=True,
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_RANK,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        lora_alpha=LORA_RANK,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    def apply_chat_template(examples):
        texts = [
            tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
            for msgs in examples["messages"]
        ]
        return {"text": texts}

    dataset = dataset.map(apply_chat_template, batched=True, remove_columns=dataset.column_names)
    split   = dataset.train_test_split(test_size=0.1, seed=42)

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LEN,
        args=SFTConfig(
            output_dir=str(output_dir / "checkpoints"),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=args.grad_accum,
            learning_rate=args.lr,
            warmup_ratio=0.05,
            lr_scheduler_type="cosine",
            fp16=True,
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            report_to="none",
        ),
    )

    print("Starting fine-tuning...")
    trainer.train()

    adapter_dir = output_dir / "lora-adapter"
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"LoRA adapter saved → {adapter_dir}")

    merge = input("\nMerge adapter and export to GGUF? [y/N] ").strip().lower()
    if merge == "y":
        merged_dir = output_dir / "merged"
        model.save_pretrained_merged(str(merged_dir), tokenizer, save_method="merged_16bit")
        gguf_path = output_dir / "o11y-agent.gguf"
        model.save_pretrained_gguf(str(gguf_path), tokenizer, quantization_method="q4_k_m")
        print(f"\nGGUF ready: {gguf_path}")
        print("Load in Ollama:")
        print(f"  echo 'FROM {gguf_path}' > Modelfile && ollama create o11y-agent -f Modelfile")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fine-tune o11y agent model")
    parser.add_argument("--mlx",    action="store_true",
                        help="Use MLX backend for Apple Silicon (Mac M1/M2/M3/M4)")
    parser.add_argument("--model",  default=None,
                        help="Override HuggingFace model ID")
    parser.add_argument("--output", default=str(OUTPUT_DIR),
                        help=f"Output directory (default: {OUTPUT_DIR})")
    # MLX-specific
    parser.add_argument("--iters",  type=int, default=MLX_ITERS,
                        help=f"MLX: training steps (default: {MLX_ITERS} ≈ 3 epochs over 1200 examples)")
    parser.add_argument("--batch",  type=int, default=MLX_BATCH,
                        help=f"Batch size (default: {MLX_BATCH})")
    parser.add_argument("--lr",     type=float, default=MLX_LR, help="Learning rate (default: 1e-5)")
    # CUDA-specific
    parser.add_argument("--epochs",     type=int, default=3)
    parser.add_argument("--grad-accum", type=int, default=4)
    args = parser.parse_args()

    if args.mlx:
        run_mlx(args)
    else:
        run_cuda(args)


if __name__ == "__main__":
    main()
