#!/usr/bin/env python3
"""
Compare the fine-tuned local model (Ollama `o11y-agent`) against the recorded
Bedrock baseline on a fixed, reproducible eval set (deploy/eval_set.jsonl).

For each of the 33 eval items (3 examples x 11 domains, seed=42):
  - "bedrock" variant = the real historical Bedrock output already recorded
    in eval_set.jsonl (no new Bedrock cost incurred).
  - "finetuned" variant = a fresh completion from the local Ollama
    `o11y-agent` model on the exact same system+user prompt.

Both variants are scored with the same Galileo metrics (one experiment per
variant), and results are aggregated per-metric and per-domain, plus a
paired win/loss/tie count using the injected eval_id to match records
across the two experiments.

Usage: .venv/bin/python3 deploy/run_eval_comparison.py
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import galileo
from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.galileo_eval import METRICS, get_or_create_project, PROJECT_NAME, _extract_metric, _wait_for_completion

EVAL_SET = Path(__file__).parent / "eval_set.jsonl"
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "o11y-agent")


def load_eval_set():
    recs = []
    with EVAL_SET.open() as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def build_messages(rec, eval_id):
    system, user, _assistant = rec["messages"][:3]
    # Append a hidden eval_id marker so every dataset input is unique — the
    # 33 eval items collapse to only 11 distinct prompts otherwise (the
    # domain task prompt doesn't embed run-specific facts), which would
    # break Galileo's dataset_input-based result matching.
    tagged_user = dict(user)
    tagged_user["content"] = user["content"] + f"\n\n[eval_id={eval_id}]"
    return [system, tagged_user]


def generate_finetuned(client, messages):
    resp = client.chat.completions.create(model=OLLAMA_MODEL, messages=messages)
    return resp.choices[0].message.content or ""


def run_experiment(tag, recs, texts):
    """Score `texts[i]` for eval item i in one Galileo experiment.

    Returns {(eval_id, domain): scores_dict}.
    """
    get_or_create_project()

    key_to_text = {}
    key_to_domain = {}
    key_to_id = {}
    key_to_messages = {}
    for i, (rec, text) in enumerate(zip(recs, texts)):
        messages = build_messages(rec, i)
        key = json.dumps(messages)
        key_to_text[key] = text
        key_to_domain[key] = rec["domain"]
        key_to_id[key] = i
        key_to_messages[key] = messages

    @galileo.log(span_type="llm")
    def score_fn(messages):
        return key_to_text[json.dumps(messages)]

    experiment_name = f"eval-{tag}-{int(time.time())}"
    galileo.experiments.run_experiment(
        experiment_name,
        project=PROJECT_NAME,
        dataset=[{"input": key_to_messages[k]} for k in key_to_text],
        function=score_fn,
        metrics=METRICS,
    )

    exp = None
    for _ in range(10):
        exp = galileo.Experiment.get(name=experiment_name, project_name=PROJECT_NAME)
        if exp is not None:
            break
        time.sleep(1)
    if exp is None:
        raise RuntimeError(f"Galileo experiment {experiment_name} not found after creation")
    # Default 180s timeout (tools/galileo_eval.py) was tuned for ~10-domain
    # runs; this eval set has 33 items, so give it more headroom.
    _wait_for_completion(exp, experiment_name, timeout=900, interval=5)

    spans_by_key = {s.get("dataset_input"): s for s in exp.get_spans() if s.get("type") == "llm"}

    results = {}
    for key, domain in key_to_domain.items():
        span = spans_by_key.get(key)
        eval_id = key_to_id[key]
        if not span:
            results[(eval_id, domain)] = {}
            continue
        scores = {}
        for metric in ("groundedness", "factuality", "completeness_gpt", "instruction_adherence"):
            status, val = _extract_metric(span, metric)
            if status == "success":
                scores[metric] = val[0] if isinstance(val, list) and val else val
        results[(eval_id, domain)] = scores
    return results


def main():
    recs = load_eval_set()
    print(f"Loaded {len(recs)} eval items ({len(set(r['domain'] for r in recs))} domains)")

    bedrock_texts = [rec["messages"][2]["content"] for rec in recs]

    print("Generating fine-tuned candidates via Ollama...")
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    finetuned_texts = []
    for i, rec in enumerate(recs):
        messages = rec["messages"][:2]
        text = generate_finetuned(client, messages)
        finetuned_texts.append(text)
        print(f"  [{i+1}/{len(recs)}] {rec['domain']}: {len(text)} chars generated")

    bedrock_cache = os.environ.get("BEDROCK_SCORES_CACHE")
    if bedrock_cache and Path(bedrock_cache).exists():
        print(f"Reusing cached Bedrock scores from {bedrock_cache} (skipping re-scoring)")
        cached = json.load(open(bedrock_cache))
        bedrock_scores = {}
        for k, v in cached.items():
            eval_id_str, domain = k.split("|", 1)
            bedrock_scores[(int(eval_id_str), domain)] = v
    else:
        print("Scoring Bedrock baseline with Galileo...")
        bedrock_scores = run_experiment("bedrock", recs, bedrock_texts)
    print("Scoring fine-tuned candidates with Galileo...")
    finetuned_scores = run_experiment("finetuned", recs, finetuned_texts)

    # Aggregate
    metrics = ("groundedness", "factuality", "completeness_gpt", "instruction_adherence")
    bedrock_sums = defaultdict(list)
    finetuned_sums = defaultdict(list)
    wins = defaultdict(int)
    losses = defaultdict(int)
    ties = defaultdict(int)

    for i, rec in enumerate(recs):
        key = (i, rec["domain"])
        b = bedrock_scores.get(key, {})
        f = finetuned_scores.get(key, {})
        for m in metrics:
            if m in b:
                bedrock_sums[m].append(b[m])
            if m in f:
                finetuned_sums[m].append(f[m])
            if m in b and m in f:
                if f[m] > b[m]:
                    wins[m] += 1
                elif f[m] < b[m]:
                    losses[m] += 1
                else:
                    ties[m] += 1

    print("\n" + "=" * 60)
    print("RESULTS: fine-tuned o11y-agent vs Bedrock baseline")
    print("=" * 60)
    for m in metrics:
        b_avg = sum(bedrock_sums[m]) / len(bedrock_sums[m]) if bedrock_sums[m] else None
        f_avg = sum(finetuned_sums[m]) / len(finetuned_sums[m]) if finetuned_sums[m] else None
        b_str = f"{b_avg:.3f}" if b_avg is not None else "n/a"
        f_str = f"{f_avg:.3f}" if f_avg is not None else "n/a"
        print(f"{m:>25}: bedrock={b_str}  finetuned={f_str}  "
              f"(wins={wins[m]} losses={losses[m]} ties={ties[m]})")

    out_path = Path(__file__).parent / "eval_comparison_results.json"
    with out_path.open("w") as f:
        json.dump({
            "bedrock_scores": {f"{k[0]}|{k[1]}": v for k, v in bedrock_scores.items()},
            "finetuned_scores": {f"{k[0]}|{k[1]}": v for k, v in finetuned_scores.items()},
            "bedrock_texts": bedrock_texts,
            "finetuned_texts": finetuned_texts,
        }, f, indent=2)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
