#!/usr/bin/env python3
"""
Export train.jsonl covering ALL labeled runs, bypassing the 20-run cap on
/api/assessment/history (receiver/otlp_receiver.py:448: `state.runs[-20:]`),
which is what /api/training/export uses under the hood via the supervisor's
get_assessment_history() — so it has always been capped to the most recent
20 runs regardless of how many runs are actually labeled.

Sources run_ids from /training/decisions (uncapped) and run detail from
/api/assessment/<run_id> (uncapped), and reproduces the same record format
as splunk-otel-supervisor/supervisor/main.py's training_export() route.
Also enriches each line with galileo_scores from deploy/galileo_scores.jsonl,
same as deploy/training_pipeline.py's export_jsonl().

Usage:
    python3 deploy/export_full_history.py
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from auto_labeler import AGENT_URL, SUPERVISOR_URL

SCRIPT_DIR = Path(__file__).parent
EXPORT_DIR = SCRIPT_DIR / "training_exports"
GALILEO_SCORES_FILE = SCRIPT_DIR / "galileo_scores.jsonl"


def _load_galileo_scores():
    """{(run_id, domain): scores_dict} from galileo_scores.jsonl, or {} if absent."""
    index = {}
    if not GALILEO_SCORES_FILE.exists():
        return index
    with GALILEO_SCORES_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            run_id, domain, scores = rec.get("run_id"), rec.get("domain"), rec.get("scores")
            if run_id and domain and scores is not None:
                index[(run_id, domain)] = scores
    return index


def main():
    r = requests.get(f"{SUPERVISOR_URL}/training/decisions", timeout=15)
    r.raise_for_status()
    decisions_index = r.json().get("index", {})  # "run_id|domain" -> decision
    all_run_ids = sorted({k.split("|")[0] for k in decisions_index})
    print(f"{len(all_run_ids)} total labeled run_ids (uncapped)")

    scores_index = _load_galileo_scores()

    lines = []
    for i, run_id in enumerate(all_run_ids, 1):
        try:
            resp = requests.get(f"{AGENT_URL}/api/assessment/{run_id}", timeout=30)
            resp.raise_for_status()
            detail = resp.json()
        except Exception as e:
            print(f"  [warn] could not fetch {run_id}: {e}")
            continue

        env = detail.get("environment", "unknown")
        specialists = detail.get("specialists", {})
        for domain, spec in specialists.items():
            raw = (spec.get("raw_text") or "").strip()
            if not raw or len(raw) < 200:
                continue
            label = decisions_index.get(f"{run_id}|{domain}", "unlabeled")
            rec = {
                "messages": [
                    {"role": "system", "content": f"You are the {domain} observability specialist for Splunk Observability Cloud."},
                    {"role": "user", "content": f"Run a complete {domain} assessment.\n\nEnvironment: {env}"},
                    {"role": "assistant", "content": raw},
                ],
                "label": label,
                "run_id": run_id,
                "domain": domain,
            }
            scores = scores_index.get((run_id, domain))
            if scores is not None:
                rec["galileo_scores"] = scores
            lines.append(rec)

        if i % 20 == 0:
            print(f"  processed {i}/{len(all_run_ids)} runs...")

    EXPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = EXPORT_DIR / f"train_{ts}_full.jsonl"
    with out_path.open("w") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")

    enriched = sum(1 for rec in lines if "galileo_scores" in rec)
    print(f"Exported {len(lines)} examples ({enriched} enriched with galileo_scores) -> {out_path}")


if __name__ == "__main__":
    main()
