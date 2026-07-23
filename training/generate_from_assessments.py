"""
Convert saved assessment detail files and captured conversations into JSONL
training data for fine-tuning a local model (e.g. qwen2.5:14b via Unsloth).

Two sources:
  1. ~/.o11y-agent/*_detail.json  — assessment outputs (instruction-following examples)
  2. ~/.o11y-agent/training/*.jsonl — full conversations captured by agent_loop.py

Output: training/data/train.jsonl  (OpenAI chat format, ready for Unsloth SFTTrainer)

Usage:
    python3 training/generate_from_assessments.py
    python3 training/generate_from_assessments.py --state-dir /path/to/.o11y-agent
"""

import argparse
import importlib
import json
import pathlib
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Specialist system prompts and tasks — loaded dynamically from the agents/ module
SPECIALISTS = ["health", "instrumentation", "governance", "detector", "logs", "rum", "rca", "synthetics", "db"]

STATE_DIR = pathlib.Path.home() / ".o11y-agent"
OUT_DIR   = pathlib.Path(__file__).parent / "data"
DEFAULT_SCORES_FILE = pathlib.Path(__file__).parent.parent / "deploy" / "galileo_scores.jsonl"


def _load_decisions(decisions_file: Optional[pathlib.Path]) -> Dict[Tuple[str, str], str]:
    """Load tuning_decisions.jsonl and return {(run_id, domain): "approve"|"reject"}.

    When a run_id has both approve and reject for the same domain (shouldn't happen
    but defensive), approve wins so we don't discard the example.
    """
    if not decisions_file or not pathlib.Path(decisions_file).exists():
        return {}

    index: Dict[Tuple[str, str], str] = {}
    with open(decisions_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            run_id = rec.get("run_id", "")
            domain = rec.get("domain", "")
            decision = rec.get("decision", "")
            if not run_id or not domain or decision not in ("approve", "reject"):
                continue
            key = (run_id, domain)
            # approve beats reject
            if index.get(key) != "approve":
                index[key] = decision
    return index


def _load_galileo_scores(scores_file: Optional[pathlib.Path]) -> Dict[Tuple[str, str], dict]:
    """Load galileo_scores.jsonl (written by deploy/auto_labeler.py) and return
    {(run_id, domain): scores_dict}. Scores are the raw Galileo metrics
    (groundedness/factuality/completeness/pii_status) captured alongside each
    approve/reject decision — kept separate from the label so callers can use
    the continuous scores later (e.g. filter/weight training data by score),
    not just the binary decision.

    Last record wins if a run_id/domain was re-scored (e.g. after a re-run).
    """
    if not scores_file or not pathlib.Path(scores_file).exists():
        return {}

    index: Dict[Tuple[str, str], dict] = {}
    with open(scores_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            run_id = rec.get("run_id", "")
            domain = rec.get("domain", "")
            scores = rec.get("scores")
            if not run_id or not domain or scores is None:
                continue
            index[(run_id, domain)] = scores
    return index


def _is_garbled_submit_findings(text: str) -> bool:
    """
    Detect the specialist tool-call corruption pattern confirmed 2026-07-22:
    raw JSON dict fragments leaking into free-text fields (e.g. a "summary"
    value that is itself literally `"submitted_run", {"severity": ...}`).
    Reuses the exact detection used for live sanitization in tools/findings.py
    so we don't fine-tune the model to reproduce the very corruption we're
    filtering out downstream — training on "approved-looking" examples that
    still contain this pattern would reinforce it.
    """
    try:
        from tools.findings import _looks_like_json_leak
    except Exception:
        return False
    return _looks_like_json_leak(text or "")


def _load_specialist_prompts() -> dict[str, dict]:
    """Import each specialist module and grab _SYSTEM + _TASK."""
    # Add repo root to path
    repo_root = pathlib.Path(__file__).parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    prompts = {}
    for name in SPECIALISTS:
        try:
            mod = importlib.import_module(f"agents.{name}")
            prompts[name] = {
                "system": getattr(mod, "_SYSTEM", "").strip(),
                "task":   getattr(mod, "_TASK",   "").strip(),
            }
        except Exception as exc:
            print(f"  warning: could not load agents.{name}: {exc}")
    return prompts


def _from_detail_file(
    path: pathlib.Path,
    prompts: dict,
    decisions: Optional[Dict[Tuple[str, str], str]] = None,
    galileo_scores: Optional[Dict[Tuple[str, str], dict]] = None,
) -> list[dict]:
    """
    Convert one *_detail.json file into instruction-following examples.
    One example per specialist + one synthesis example.

    decisions: index from _load_decisions(). When provided, each example gets a
    "label" field: "approved", "rejected", or "unlabeled". The finetune pipeline
    can filter or oversample by label.

    galileo_scores: index from _load_galileo_scores(). When provided and a match
    exists, each example also gets a "galileo_scores" field (groundedness/
    factuality/completeness/pii_status) alongside the label.
    """
    examples = []
    try:
        detail = json.loads(path.read_text())
    except Exception as exc:
        print(f"  skip {path.name}: {exc}")
        return []

    env = detail.get("environment", "unknown")
    run_id = str(detail.get("run_id") or detail.get("id") or "")
    specialists = detail.get("specialists", {})

    # --- Per-specialist examples ---
    for name, spec in specialists.items():
        raw = (spec.get("raw_text") or "").strip()
        if not raw or raw in ("timeout", ) or raw.startswith("["):
            continue  # skip errors/timeouts
        if len(raw) < 200:
            continue  # too short to be useful
        if _is_garbled_submit_findings(raw):
            continue  # skip corrupted specialist output — don't train on it

        p = prompts.get(name, {})
        system = p.get("system") or f"You are the {name} observability specialist for Splunk Observability Cloud."
        task   = p.get("task")   or f"Run a complete {name} assessment for the environment."

        label = "unlabeled"
        if decisions and run_id:
            label = decisions.get((run_id, name), "unlabeled")

        example = {
            "messages": [
                {"role": "system",    "content": system},
                {"role": "user",      "content": f"{task}\n\nEnvironment: {env}"},
                {"role": "assistant", "content": raw},
            ],
            "label": label,
            "run_id": run_id,
            "domain": name,
        }
        if galileo_scores and run_id:
            scores = galileo_scores.get((run_id, name))
            if scores is not None:
                example["galileo_scores"] = scores
        examples.append(example)

    # --- Synthesis example ---
    synthesis = (detail.get("synthesis") or "").strip()
    if synthesis and len(synthesis) > 500:
        summaries = "\n\n".join(
            f"### {name.upper()} SPECIALIST\n{spec.get('summary', '')}"
            for name, spec in specialists.items()
            if spec.get("summary")
        )
        cross = (detail.get("cross_domain") or "").strip()
        user_content = f"Environment: {env}\n\n{summaries}"
        if cross:
            user_content += f"\n\n{cross}"

        # Synthesis is labeled "approved" if any specialist in the run was approved
        synth_label = "unlabeled"
        if decisions and run_id:
            any_approved = any(
                decisions.get((run_id, d)) == "approve" for d in specialists
            )
            synth_label = "approved" if any_approved else "unlabeled"

        from agents.coordinator import _SYNTHESIS_SYSTEM
        examples.append({
            "messages": [
                {"role": "system",    "content": _SYNTHESIS_SYSTEM.strip()},
                {"role": "user",      "content": user_content},
                {"role": "assistant", "content": synthesis},
            ],
            "label": synth_label,
            "run_id": run_id,
            "domain": "synthesis",
        })

    return examples


def _from_conversation_file(path: pathlib.Path) -> list[dict]:
    """
    Convert a captured full-conversation JSONL file into a multi-turn chat example.
    These are richer — they include tool calls and results.
    """
    try:
        record = json.loads(path.read_text())
    except Exception:
        return []

    system = record.get("system", "")
    messages = record.get("messages", [])
    if not system or not messages:
        return []

    # Convert Bedrock-style messages to OpenAI chat format
    chat = [{"role": "system", "content": system}]
    has_garbled_submit = False
    submit_findings_called = False
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])

        if role == "user":
            # Extract text from content blocks
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and "text" in c]
            # Tool results
            tool_parts = [
                f"[tool_result:{c.get('toolResult', {}).get('toolUseId', '')}] "
                + str(c.get("toolResult", {}).get("content", ""))
                for c in content if isinstance(c, dict) and "toolResult" in c
            ]
            combined = "\n".join(text_parts + tool_parts).strip()
            if combined:
                chat.append({"role": "user", "content": combined})

        elif role == "assistant":
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and "text" in c]
            tool_calls = []
            for c in content:
                if not (isinstance(c, dict) and "toolUse" in c):
                    continue
                tu = c["toolUse"]
                if tu.get("name") == "submit_findings":
                    submit_findings_called = True
                    inp = tu.get("input", {}) or {}
                    fields_to_check = [inp.get("summary", "")]
                    for issue in inp.get("issues", []) or []:
                        if isinstance(issue, dict):
                            fields_to_check.append(issue.get("description", ""))
                            fields_to_check.append(issue.get("recommendation", ""))
                    # Fields are normally strings, but the model sometimes passes a
                    # list/dict instead (same malformation class as tools/findings.py
                    # defends against live) — coerce so this check doesn't crash on it.
                    fields_to_check = [t if isinstance(t, str) else json.dumps(t) for t in fields_to_check]
                    if any(_is_garbled_submit_findings(t) for t in fields_to_check):
                        has_garbled_submit = True
                tool_calls.append(f"[tool_call:{tu.get('name', '')}] " + json.dumps(tu.get("input", {})))
            combined = "\n".join(text_parts + tool_calls).strip()
            if combined:
                chat.append({"role": "assistant", "content": combined})

    if has_garbled_submit:
        return []  # skip whole example — don't train the model to reproduce this corruption

    # If submit_findings was an available tool but the model never actually invoked
    # it (narrated the intended call as plain text/JSON instead, or just rambled —
    # confirmed 2026-07-23 root cause of recurring "rambling scratchpad" specialist
    # output, e.g. logs: "Let's call `get_non_critical_errors` now."), don't train
    # on it. Unlike _from_detail_file, this path never applied approve/reject labels
    # at all, so these bad conversations were being included at full weight in every
    # fine-tune — reinforcing the exact behavior we don't want.
    tool_names = record.get("tool_names") or []
    if "submit_findings" in tool_names and not submit_findings_called:
        return []

    if len(chat) < 3:  # system + at least one exchange
        return []

    return [{"messages": chat}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=str(STATE_DIR))
    parser.add_argument(
        "--decisions-file", default=None,
        help="Path to tuning_decisions.jsonl from supervisor training_store. "
             "Adds a 'label' field (approved/rejected/unlabeled) to each example.",
    )
    parser.add_argument(
        "--scores-file", default=str(DEFAULT_SCORES_FILE),
        help="Path to galileo_scores.jsonl (written by deploy/auto_labeler.py). "
             f"Adds a 'galileo_scores' field to each example. Default: {DEFAULT_SCORES_FILE}",
    )
    args = parser.parse_args()

    state_dir = pathlib.Path(args.state_dir)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "train.jsonl"

    print("Loading specialist prompts...")
    prompts = _load_specialist_prompts()
    print(f"  loaded prompts for: {', '.join(prompts)}")

    decisions = _load_decisions(args.decisions_file)
    if decisions:
        approved = sum(1 for v in decisions.values() if v == "approve")
        rejected = sum(1 for v in decisions.values() if v == "reject")
        print(f"Loaded {len(decisions)} decision records ({approved} approved, {rejected} rejected)")
    else:
        print("No decisions file — all examples will be labeled 'unlabeled'")

    galileo_scores = _load_galileo_scores(args.scores_file)
    if galileo_scores:
        print(f"Loaded {len(galileo_scores)} Galileo score records from {args.scores_file}")
    else:
        print(f"No Galileo scores found at {args.scores_file} — examples won't have galileo_scores")

    examples = []

    # Source 1: assessment detail files. Includes both the single rolling
    # "*_detail.json" file and the per-run "*_detail_run_<hex>.json" snapshots
    # (500 confirmed present 2026-07-23 — previously silently skipped since the
    # glob only matched the rolling file). The rolling file's run_id can
    # duplicate one of the per-run snapshots, so dedup by run_id.
    detail_files = list(state_dir.glob("*_detail.json")) + list(state_dir.glob("*_detail_run_*.json"))
    print(f"\nProcessing {len(detail_files)} assessment detail file(s)...")
    seen_run_ids = set()
    for f in detail_files:
        try:
            run_id = json.loads(f.read_text()).get("run_id")
        except Exception:
            run_id = None
        if run_id and run_id in seen_run_ids:
            print(f"  {f.name}: skipped (duplicate run_id {run_id})")
            continue
        if run_id:
            seen_run_ids.add(run_id)
        ex = _from_detail_file(f, prompts, decisions, galileo_scores)
        print(f"  {f.name}: {len(ex)} examples")
        examples.extend(ex)

    # Source 2: captured conversation files
    conv_dir = state_dir / "training"
    conv_files = list(conv_dir.glob("*.jsonl")) if conv_dir.exists() else []
    print(f"\nProcessing {len(conv_files)} captured conversation(s)...")
    for f in conv_files:
        ex = _from_conversation_file(f)
        if ex:
            examples.extend(ex)

    # Write output
    with out_path.open("w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")

    print(f"\nWrote {len(examples)} training examples → {out_path}")
    print("\nExample breakdown:")
    print(f"  From detail files:    {sum(1 for e in examples if len(e['messages']) <= 3)}")
    print(f"  From conversations:   {sum(1 for e in examples if len(e['messages']) > 3)}")
    if decisions:
        print("\nLabel breakdown:")
        for label in ("approved", "rejected", "unlabeled"):
            n = sum(1 for e in examples if e.get("label") == label)
            print(f"  {label:12s}: {n}")


if __name__ == "__main__":
    main()
