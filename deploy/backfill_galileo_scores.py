#!/usr/bin/env python3
"""
Backfill Galileo scores for historical assessment runs.

New assessment runs are currently blocked (AWS credentials expired), so to
build up enough Galileo-scored training data for fine-tuning, this scores
runs that were already decided (approve/reject) by auto_labeler.py in the
past but never had their Galileo numeric scores captured — either because
they predate the score-persistence feature, or because they fell outside
the supervisor's 20-run export window when auto_labeler last ran.

Only appends to galileo_scores.jsonl. Does NOT re-post decisions to the
supervisor — those already exist in the decisions index and shouldn't be
overwritten by a backfill pass.

Usage:
    python3 deploy/backfill_galileo_scores.py --dry-run   # scan + report only
    python3 deploy/backfill_galileo_scores.py --limit 10  # backfill 10 runs
    python3 deploy/backfill_galileo_scores.py              # backfill everything
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from auto_labeler import (
    AGENT_URL, SUPERVISOR_URL, ENV_FACTS, DOMAINS,
    GALILEO_SCORES_FILE, log_galileo_score,
)
from tools import galileo_eval


def _already_scored_pairs():
    """{(run_id, domain)} already present in galileo_scores.jsonl."""
    scored = set()
    if not os.path.exists(GALILEO_SCORES_FILE):
        return scored
    with open(GALILEO_SCORES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            scored.add((rec.get("run_id"), rec.get("domain")))
    return scored


def _get_all_run_ids():
    r = requests.get(f"{SUPERVISOR_URL}/training/decisions", timeout=15)
    r.raise_for_status()
    index = r.json().get("index", {})
    return sorted({k.split("|")[0] for k in index})


def _get_detail(run_id):
    try:
        r = requests.get(f"{AGENT_URL}/api/assessment/{run_id}", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] could not fetch {run_id}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Backfill Galileo scores for historical runs")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of runs to backfill this pass")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report scope only, don't call Galileo")
    args = parser.parse_args()

    scored_pairs = _already_scored_pairs()
    run_ids = _get_all_run_ids()
    print(f"{len(run_ids)} total historical run_ids in decisions index")
    print(f"{len({r for r, d in scored_pairs})} run_ids already have some Galileo scores "
          f"({len(scored_pairs)} domain-records)")

    todo = []
    for rid in run_ids:
        detail = _get_detail(rid)
        if not detail:
            continue
        specs = detail.get("specialists", {})
        pending = [d for d in DOMAINS if specs.get(d) and (rid, d) not in scored_pairs]
        if pending:
            todo.append((rid, detail, pending))

    total_pending_domains = sum(len(p) for _, _, p in todo)
    print(f"{len(todo)} runs with unscored domain(s) ({total_pending_domains} domain-records total)")

    if args.dry_run:
        return

    if args.limit:
        todo = todo[:args.limit]
        print(f"Limiting this pass to {len(todo)} run(s)")

    scored_runs = 0
    scored_domains = 0
    for rid, detail, pending in todo:
        specs = detail.get("specialists", {})
        print(f"[{rid}] scoring {len(pending)} domain(s): {pending}")
        try:
            decisions = galileo_eval.evaluate_run(rid, specs, ENV_FACTS, pending)
        except Exception as e:
            print(f"  [error] Galileo eval failed for {rid}: {e}")
            continue
        for domain, (decision, reason, scores) in decisions.items():
            log_galileo_score(rid, domain, decision, reason, scores)
            marker = "\u2713" if decision == "approve" else "\u2717"
            print(f"    {marker} {domain:16s} [{decision}]  {reason}")
            scored_domains += 1
        scored_runs += 1

    print(f"\nBackfilled {scored_domains} domain-records across {scored_runs} run(s).")


if __name__ == "__main__":
    main()
