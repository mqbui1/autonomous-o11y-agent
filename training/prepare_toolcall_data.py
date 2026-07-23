#!/usr/bin/env python3
"""
Convert raw agent_loop.py training captures (Bedrock-native multi-turn tool-calling
conversations, saved when CAPTURE_TRAINING_DATA=true) into Qwen2.5 tool-calling
chat format for mlx_lm fine-tuning.

Why: the existing export (deploy/export_full_history.py / training_pipeline.py)
only keeps each specialist's FINAL text output as a single-turn
system/user/assistant example. That taught the model good prose, but never the
tool-calling protocol itself -- confirmed by the 2026-07-21 production regression
(see training.md) where the fine-tuned model, plugged into the real agent loop,
over-generated tool calls and leaked raw <tool_call> JSON into final answers.

This script instead emits ONE training row per assistant turn in each captured
conversation (both intermediate tool-call turns and the final text turn), so
mask_prompt-based SFT trains the model to produce every turn a real
specialist run actually needs -- not just the last one.

Label/quality weighting is only meaningful for the final text turn (that's the
only thing Galileo scored / a human labeled). Intermediate tool-call turns
represent real production Bedrock tool-selection behavior and are kept at
weight 1 regardless of the final output's label, since a bad final report
doesn't imply bad tool selection.

Usage:
    python3 training/prepare_toolcall_data.py \
        --captures training/data/raw_captures \
        --labeled-export deploy/training_exports/train_20260720_151646.jsonl \
        --out training/data/toolcall_train.jsonl
"""
import argparse
import hashlib
import importlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Captures only store `tool_names` (a list of names), not the full toolSpec
# (with inputSchema) that was actually sent to the model at generation time.
# Reconstruct name -> toolSpec by importing every tools/*.py module that
# exports a SCHEMAS (or SUBMIT_SCHEMA) list and merging them by name -- this
# mirrors exactly what each agents/*.py module assembles at runtime.
_SCHEMA_MODULES = [
    "tools.rum_analyzer", "tools.log_analyzer", "tools.governance",
    "tools.profiling_tools", "tools.source_tools", "tools.rca_tools",
    "tools.db_tools", "tools.analyzer", "tools.findings",
    "tools.synthetics_tools", "tools.dashboard", "tools.provisioner",
    "tools.health_check", "tools.adoption_tools",
]


def _build_toolspec_registry() -> dict[str, dict]:
    registry = {}
    for modname in _SCHEMA_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            print(f"  [warn] could not import {modname}: {e}")
            continue
        for spec in getattr(mod, "SCHEMAS", []):
            name = spec.get("toolSpec", {}).get("name")
            if name:
                registry[name] = spec
        submit = getattr(mod, "SUBMIT_SCHEMA", None)
        if submit:
            name = submit.get("toolSpec", {}).get("name")
            if name:
                registry[name] = submit
    return registry


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def _load_label_index(export_path: pathlib.Path) -> dict[str, tuple]:
    """hash(final assistant text) -> (label, galileo_scores) from the existing
    single-shot labeled export."""
    index = {}
    if not export_path.exists():
        print(f"WARNING: labeled export not found: {export_path} (no labels will be joined)")
        return index
    with export_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            raw = row["messages"][-1]["content"]
            index[_hash(raw)] = (row.get("label", "unlabeled"), row.get("galileo_scores"))
    return index


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Bedrock toolSpec format -> OpenAI function format (matches
    providers/openai_compat.py's OpenAICompatProvider.convert_tools, which is
    also what the Qwen2.5 chat template expects)."""
    converted = []
    for tool in tools:
        spec = tool.get("toolSpec", {})
        schema = spec.get("inputSchema", {}).get("json", {})
        converted.append({
            "type": "function",
            "function": {
                "name": spec.get("name", ""),
                "description": spec.get("description", ""),
                "parameters": schema,
            },
        })
    return converted


def _extract_text(content_blocks) -> str:
    """Concatenate {"text": ...} blocks from a Bedrock content-block list."""
    if isinstance(content_blocks, str):
        return content_blocks
    return "".join(b.get("text", "") for b in content_blocks if isinstance(b, dict) and "text" in b)


def _convert_messages(raw_messages: list[dict]) -> list[dict]:
    """Bedrock-native message list (content-block based) -> flat OpenAI-ish
    role/content(+tool_calls) messages matching the Qwen2.5 chat template:
      - user (initial)      -> {"role": "user", "content": str}
      - assistant w/ tools   -> {"role": "assistant", "content": str,
                                  "tool_calls": [{"id","type":"function","function":{"name","arguments"}}]}
      - assistant text-only  -> {"role": "assistant", "content": str}
      - tool results (were "user" role w/ toolResult blocks) -> one
                                  {"role": "tool", "tool_call_id", "content"} per block
    """
    out = []
    for m in raw_messages:
        role = m.get("role")
        content = m.get("content", [])
        if role == "user" and isinstance(content, list) and content and \
                all(isinstance(b, dict) and "toolResult" in b for b in content):
            for b in content:
                tr = b["toolResult"]
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.get("toolUseId", ""),
                    "content": _extract_text(tr.get("content", [])),
                })
            continue

        if role == "user":
            out.append({"role": "user", "content": _extract_text(content)})
            continue

        if role == "assistant":
            text = _extract_text(content)
            tool_calls = [
                {
                    "id": b["toolUse"].get("toolUseId", ""),
                    "type": "function",
                    "function": {
                        "name": b["toolUse"].get("name", ""),
                        "arguments": b["toolUse"].get("input", {}),
                    },
                }
                for b in content if isinstance(b, dict) and "toolUse" in b
            ]
            msg = {"role": "assistant", "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

    return out


def _submit_findings_is_garbled(msg: dict) -> bool:
    """
    True if this assistant turn has the specialist-level corruption confirmed
    2026-07-22 (see training.md): raw JSON dict fragments leaking into
    summary/description/recommendation text.

    Checks two places:
      1. A parsed `submit_findings` tool call's arguments (the case originally
         assumed here).
      2. The turn's raw text content (the case actually confirmed against real
         production capture 2036d10b5fc7.jsonl, same timestamp as the
         2026-07-22 live regression): the model failed to emit a real tool
         call at all and instead dumped a garbled JSON/JS-like blob -- e.g.
         literally `"submitted_run", {\"severity\": \"critical\", ...` -- as
         plain assistant text. A tool_calls-only check misses this entirely,
         which is why the filter measured 0% detection against 3200+ real
         captures until this was added.

    Reuses the exact detection used for live inference-time sanitization in
    tools/findings.py so we don't fine-tune the model to reproduce the very
    corruption that sanitization step exists to clean up.
    """
    try:
        from tools.findings import _looks_like_json_leak
    except Exception:
        return False
    if _looks_like_json_leak(msg.get("content") or ""):
        return True
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        if fn.get("name") != "submit_findings":
            continue
        args = fn.get("arguments", {}) or {}

        def _as_text(v):
            # The model occasionally passes a dict/list where a string is
            # expected (same malformed shape tools/findings.py already
            # defends against at inference time) -- coerce instead of
            # crashing _looks_like_json_leak's regex match.
            if isinstance(v, str):
                return v
            return json.dumps(v) if v else ""

        fields = [_as_text(args.get("summary", ""))]
        for issue in args.get("issues", []) or []:
            if isinstance(issue, dict):
                fields.append(_as_text(issue.get("description", "")))
                fields.append(_as_text(issue.get("recommendation", "")))
        if any(_looks_like_json_leak(t) for t in fields):
            return True
    return False


def _weight_count(label: str, scores: dict | None) -> int:
    """Same policy as finetune.py's _weight_count, applied only to the final
    text turn of a conversation."""
    if label in ("reject", "rejected"):
        return 0
    scores = scores or {}
    if scores.get("pii_status") not in (None, "Not Found"):
        return 0
    vals = [scores.get(k) for k in
            ("groundedness", "factuality", "completeness", "instruction_adherence")
            if scores.get(k) is not None]
    if vals:
        quality = sum(vals) / len(vals)
        if quality < 0.5:
            return 0
        return 2 if quality >= 0.9 else 1
    return 2 if label in ("approve", "approved") else 1


def convert_capture(rec: dict, label_index: dict, toolspec_registry: dict) -> list[dict]:
    """One captured conversation -> a list of training rows, one per
    assistant turn (intermediate tool-call turns + the final text turn)."""
    system = rec.get("system", "")
    bedrock_specs = [
        toolspec_registry[name] for name in rec.get("tool_names", [])
        if name in toolspec_registry
    ]
    tools_oai = _convert_tools(bedrock_specs) if bedrock_specs else None

    raw_messages = rec.get("messages", [])
    converted = _convert_messages(raw_messages)
    full = [{"role": "system", "content": system}] + converted

    if any(m.get("role") == "assistant" and _submit_findings_is_garbled(m) for m in full):
        return []  # skip whole conversation -- don't train on corrupted submit_findings output

    final_text = rec.get("final_text", "")
    label, scores = label_index.get(_hash(final_text), ("unlabeled", None))

    rows = []
    for i, msg in enumerate(full):
        if msg["role"] != "assistant":
            continue
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            # Degenerate turn: no text and no tool calls. Under mask_prompt, the
            # loss window for this row is fully masked (0 unmasked tokens), which
            # makes mlx_lm's cross-entropy computation divide by zero -> NaN.
            # Confirmed root cause of repeated NaN divergence at iter 113 (2026-07-21/22).
            continue
        is_final = (i == len(full) - 1)
        row_messages = full[: i + 1]
        row = {"messages": row_messages, "id": rec.get("id"), "turn_index": i}
        if tools_oai:
            row["tools"] = tools_oai
        if is_final:
            row["label"] = label
            if scores:
                row["galileo_scores"] = scores
            row["_weight"] = _weight_count(label, scores)
        else:
            row["label"] = "unlabeled"
            row["_weight"] = 1  # tool-call turns always kept, regardless of final label
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", default="training/data/raw_captures")
    ap.add_argument("--labeled-export", default="deploy/training_exports/train_20260720_151646.jsonl")
    ap.add_argument("--out", default="training/data/toolcall_train.jsonl")
    args = ap.parse_args()

    cap_dir = pathlib.Path(args.captures)
    if not cap_dir.exists():
        sys.exit(f"Captures dir not found: {cap_dir}")

    label_index = _load_label_index(pathlib.Path(args.labeled_export))
    print(f"Loaded {len(label_index)} labeled final-turn hashes for joining")

    toolspec_registry = _build_toolspec_registry()
    print(f"Built toolSpec registry: {len(toolspec_registry)} known tool names")

    all_rows = []
    n_captures = 0
    n_matched = 0
    for p in cap_dir.glob("*.jsonl"):
        try:
            rec = json.loads(p.read_text())
        except Exception as e:
            print(f"  [warn] skip {p.name}: {e}")
            continue
        n_captures += 1
        rows = convert_capture(rec, label_index, toolspec_registry)
        if rows and rows[-1].get("label") not in (None, "unlabeled"):
            n_matched += 1
        all_rows.extend(rows)

    excluded = sum(1 for r in all_rows if r["_weight"] == 0)
    kept = [r for r in all_rows if r["_weight"] > 0]
    weighted_out = []
    for r in kept:
        w = r.pop("_weight")
        # Weighting (0/1/2x oversampling) is fully baked in via duplication here.
        # Strip label/galileo_scores so finetune.py's _load_all_examples doesn't
        # re-apply its own _weight_count() on top of this and double-oversample.
        r.pop("label", None)
        r.pop("galileo_scores", None)
        weighted_out.extend([r] * w)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for row in weighted_out:
            f.write(json.dumps(row) + "\n")

    print(f"Captures processed: {n_captures} ({n_matched} joined to a final-turn label)")
    print(f"Raw per-turn rows generated: {len(all_rows)} ({excluded} excluded by weight=0)")
    print(f"Final effective rows written (after 2x oversampling): {len(weighted_out)} -> {out_path}")


if __name__ == "__main__":
    main()
