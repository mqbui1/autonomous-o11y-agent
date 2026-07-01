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

# Specialist system prompts and tasks — loaded dynamically from the agents/ module
SPECIALISTS = ["health", "instrumentation", "governance", "detector", "logs", "rum", "rca", "synthetics", "db"]

STATE_DIR = pathlib.Path.home() / ".o11y-agent"
OUT_DIR   = pathlib.Path(__file__).parent / "data"


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


def _from_detail_file(path: pathlib.Path, prompts: dict) -> list[dict]:
    """
    Convert one *_detail.json file into instruction-following examples.
    One example per specialist + one synthesis example.
    """
    examples = []
    try:
        detail = json.loads(path.read_text())
    except Exception as exc:
        print(f"  skip {path.name}: {exc}")
        return []

    env = detail.get("environment", "unknown")
    specialists = detail.get("specialists", {})

    # --- Per-specialist examples ---
    for name, spec in specialists.items():
        raw = (spec.get("raw_text") or "").strip()
        if not raw or raw in ("timeout", ) or raw.startswith("["):
            continue  # skip errors/timeouts
        if len(raw) < 200:
            continue  # too short to be useful

        p = prompts.get(name, {})
        system = p.get("system") or f"You are the {name} observability specialist for Splunk Observability Cloud."
        task   = p.get("task")   or f"Run a complete {name} assessment for the environment."

        examples.append({
            "messages": [
                {"role": "system",    "content": system},
                {"role": "user",      "content": f"{task}\n\nEnvironment: {env}"},
                {"role": "assistant", "content": raw},
            ]
        })

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

        from agents.coordinator import _SYNTHESIS_SYSTEM
        examples.append({
            "messages": [
                {"role": "system",    "content": _SYNTHESIS_SYSTEM.strip()},
                {"role": "user",      "content": user_content},
                {"role": "assistant", "content": synthesis},
            ]
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
            tool_calls = [
                f"[tool_call:{c.get('toolUse', {}).get('name', '')}] "
                + json.dumps(c.get("toolUse", {}).get("input", {}))
                for c in content if isinstance(c, dict) and "toolUse" in c
            ]
            combined = "\n".join(text_parts + tool_calls).strip()
            if combined:
                chat.append({"role": "assistant", "content": combined})

    if len(chat) < 3:  # system + at least one exchange
        return []

    return [{"messages": chat}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=str(STATE_DIR))
    args = parser.parse_args()

    state_dir = pathlib.Path(args.state_dir)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "train.jsonl"

    print("Loading specialist prompts...")
    prompts = _load_specialist_prompts()
    print(f"  loaded prompts for: {', '.join(prompts)}")

    examples = []

    # Source 1: assessment detail files
    detail_files = list(state_dir.glob("*_detail.json"))
    print(f"\nProcessing {len(detail_files)} assessment detail file(s)...")
    for f in detail_files:
        ex = _from_detail_file(f, prompts)
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


if __name__ == "__main__":
    main()
