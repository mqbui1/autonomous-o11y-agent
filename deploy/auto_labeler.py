#!/usr/bin/env python3
"""
Auto-labeler for o11y-agent assessments.

Polls for new assessment runs and applies approve/reject labels based on
known facts about the astroshop-local environment.

Usage:
    python3 auto_labeler.py [--once]   # --once labels all current runs then exits
"""

import argparse
import json
import os
import sys
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

AGENT_URL      = "http://localhost:4319"
SUPERVISOR_URL = "http://localhost:9090/api"

# The supervisor's /training/label endpoint only stores the decision, not the
# underlying Galileo scores. Persist scores here so generate_from_assessments.py
# can attach them to training examples alongside the approve/reject label.
GALILEO_SCORES_FILE = os.path.join(os.path.dirname(__file__), "galileo_scores.jsonl")

# EVAL_ENGINE=galileo (default) scores specialist output via the Galileo API
# (context_adherence/correctness/completeness/output_pii — no UI involved).
# EVAL_ENGINE=rules falls back to the original hand-written evaluate() below.
EVAL_ENGINE = os.environ.get("EVAL_ENGINE", "galileo")

# Known facts about this environment — used to detect wrong recommendations
ENV_FACTS = {
    "is_kubernetes":     False,  # Docker Compose, not K8s
    "has_log_observer":  False,  # HTTP 404 on all log APIs — not licensed
    "has_synthetics":    False,  # HTTP 403 — no Synthetics entitlement
    "has_rum":           False,  # No browser sessions Phase 0-2; True in Phase 3+ (browser-sim)
    "environment":       "astroshop-local",
}

DOMAINS = ["health", "instrumentation", "governance", "detector",
           "logs", "rum", "rca", "synthetics", "db", "performance"]


# ── API helpers ───────────────────────────────────────────────────────────────

def _get(url, timeout=15):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _post(url, timeout=10):
    r = requests.post(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def get_labeled_index():
    """Return set of already-labeled 'run_id|domain' keys."""
    try:
        data = _get(f"{SUPERVISOR_URL}/training/decisions")
        return set(data.get("index", {}).keys())
    except Exception as e:
        print(f"  [warn] could not fetch decisions: {e}")
        return set()

def get_history():
    try:
        data = _get(f"{AGENT_URL}/api/assessment/history")
        return data.get("runs", [])
    except Exception as e:
        print(f"  [warn] could not fetch history: {e}")
        return []

def get_detail(run_id):
    try:
        data = _get(f"{AGENT_URL}/api/assessment/{run_id}", timeout=30)
        return data
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return None
        print(f"  [warn] HTTP error fetching {run_id}: {e}")
        return None
    except Exception as e:
        print(f"  [warn] could not fetch {run_id}: {e}")
        return None

def post_label(run_id, domain, decision):
    try:
        _post(f"{SUPERVISOR_URL}/training/label/{run_id}/{domain}/{decision}")
    except Exception as e:
        print(f"  [error] could not post label {domain}={decision}: {e}")

def log_galileo_score(run_id, domain, decision, reason, scores):
    """Append one record to galileo_scores.jsonl (only called for Galileo-scored domains)."""
    record = {
        "run_id": run_id,
        "domain": domain,
        "decision": decision,
        "reason": reason,
        "scores": scores,
        "scored_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    try:
        with open(GALILEO_SCORES_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"  [warn] could not write galileo_scores.jsonl: {e}")


# ── Labeling rules ────────────────────────────────────────────────────────────

def _active_count(specialists):
    """Max active service count across all specialists."""
    count = 0
    for s in specialists.values():
        n = s.get("services_active") or []
        count = max(count, len(n) if isinstance(n, list) else int(n))
    return count

def evaluate(domain, spec, detail):
    """
    Return (decision, reason).
    decision: 'approve' | 'reject'
    """
    raw    = (spec.get("raw_text") or "").strip()
    issues = spec.get("issues") or []
    specs  = detail.get("specialists", {})
    active = _active_count(specs)

    # ── Universal garbage filters — applied before any domain logic ──────────────

    # 1. Too short — tooling failure, timeout, or AWS credential expiry
    if not raw or len(raw) < 150:
        return "reject", "specialist returned no usable output (timeout or tooling failure)"

    # 2. Agent runtime error — Bedrock/Converse API exceptions, ValidationException, etc.
    #    These strings appear when the agent_loop crashes before producing real analysis.
    AGENT_ERROR_PATTERNS = [
        "agent error:",
        "An error occurred (ValidationException)",
        "An error occurred (ThrottlingException)",
        "An error occurred (ModelErrorException)",
        "An error occurred (ServiceUnavailableException)",
        "AWS credentials expired or invalid",
        "ExpiredTokenException",
        "UnrecognizedClientException",
        "Agent reached max turns without completing",
    ]
    for pattern in AGENT_ERROR_PATTERNS:
        if pattern.lower() in raw.lower():
            return "reject", f"agent runtime error — not real analysis: {pattern}"

    # 3. Narrate-instead-of-invoke — the model describes calling a tool in prose
    #    instead of actually calling it, producing rambling scratchpad text that
    #    reads like real analysis but never yielded structured findings (the
    #    specialist's own fallback then truncates it via raw_text[:500]). Confirmed
    #    2026-07-23 recurring on logs ("Let's call `get_non_critical_errors` now.")
    #    and rca. The old logs rule below blanket-approved any output that mentioned
    #    "404"/"Log Observer" regardless of this — checked before domain logic so
    #    every domain benefits.
    RAMBLING_MARKERS = [
        "let's call", "let me call", "i'll call", "i will call",
        "let's use the tool", "let me use the tool", "i'll use the tool", "i will use the tool",
        "let's invoke", "let me invoke", "i'll invoke", "i will invoke",
        "next i will", "next, i will", "let's take action now",
    ]
    if any(m in raw.lower() for m in RAMBLING_MARKERS):
        return "reject", "specialist narrated tool calls as prose instead of invoking them (rambling scratchpad output)"

    if domain == "instrumentation":
        # Reject if it contradicts itself: claims service.name absent from 100% of
        # spans yet health/other specialists show multiple active named services.
        claims_total_absence = any(
            "service.name" in i.get("description", "") and
            ("100%" in i.get("description", "") or "missing from 100" in i.get("description", ""))
            for i in issues
        )
        if claims_total_absence and active >= 5:
            return "reject", (
                f"claims service.name missing from 100% of spans but "
                f"{active} named active services detected — internal contradiction"
            )
        # Reject if the bulk of recommendations are K8s-specific (wrong environment type)
        k8s_recs = sum(
            1 for i in issues
            if "k8sattributes" in i.get("recommendation", "")
            or "k8s.pod.name" in i.get("description", "")
            or "k8s.node.name" in i.get("description", "")
        )
        if k8s_recs >= 4 and not ENV_FACTS["is_kubernetes"]:
            return "reject", (
                f"{k8s_recs} K8s-specific recommendations for a Docker Compose environment — "
                "wrong environment type assumption"
            )
        return "approve", "instrumentation findings consistent with observable environment state"

    if domain == "detector":
        # Reject only if the report is NOT grounded in our own environment — i.e. the
        # specialist analyzed the wrong environment entirely. Mentioning other orgs'
        # detector names (e.g. "Nuveen SOR-Data pipeline detectors") as a specific
        # finding within a correctly-scoped astroshop-local report is a genuine,
        # valuable finding (cross-tenant detector-listing leak), NOT hallucination —
        # do not reject for that.
        cross_env_markers = ["Nuveen", "SOR-Data", "petclinic", "te-o11y",
                             "travelplanner", "Travelplanner"]
        found = [m for m in cross_env_markers if m in raw]
        grounded = ENV_FACTS["environment"] in raw or "astroshop" in raw.lower()
        if found and not grounded:
            return "reject", f"cross-environment contamination — report never grounds itself in {ENV_FACTS['environment']}, only discusses: {found}"
        return "approve", "detector analysis correctly scoped to this environment"

    if domain == "logs":
        # Approve if it correctly identifies Log Observer as unavailable
        if "404" in raw or "Log Observer" in raw:
            return "approve", "correctly identifies Log Observer as unavailable (HTTP 404)"
        return "approve", "log analysis consistent with environment state"

    if domain == "synthetics":
        # Always approve — HTTP 403 is factually correct for this account
        return "approve", "correctly identifies Synthetics as unavailable (HTTP 403 no entitlement)"

    if domain == "rum":
        # After Phase 3 the browser-sim generates real sessions; detect which state we're in
        # by checking whether the specialist found any session/vitals data in the output.
        active_rum_keywords = [
            "session", "page view", "pageview", "lcp", "fid", "cls", "inp",
            "core web vital", "javascript error", "js error", "xhr", "fetch",
            "navigation", "long task", "resource timing", "web vital",
        ]
        has_active_data = any(kw in raw.lower() for kw in active_rum_keywords)

        if has_active_data:
            # RUM is active — approve if it correctly analyzes the session data
            return "approve", "correctly analyzes active RUM session data and Core Web Vitals"

        # No session data found — correct for Phase 0-2 (browser sim not yet running)
        return "approve", "correctly identifies zero RUM sessions (browser sim not yet active)"

    if domain == "governance":
        # Approve if PII findings or tooling errors accurately reported
        has_pii = any(
            "PII" in i.get("description", "") or
            "IPv4" in i.get("description", "") or
            "private" in i.get("description", "").lower()
            for i in issues
        )
        has_tooling_error = "SyntaxError" in raw or "tooling" in raw.lower()
        if has_pii or has_tooling_error:
            return "approve", "accurately reports PII in-flight detections and/or tooling errors"
        # Reject if governance returned entirely no data
        if "NO DATA" in raw.upper() or not issues:
            return "reject", "governance returned no findings (tooling completely offline)"
        return "approve", "governance analysis consistent"

    if domain == "health":
        # Health uses streaming pipeline data — reliable
        return "approve", "health analysis uses reliable streaming pipeline data"

    if domain == "rca":
        # Approve — error rate and latency analysis is straightforward
        return "approve", "RCA findings consistent with observable service state"

    if domain == "db":
        # Approve if it catches flagd error rate or db attribute gaps
        has_flagd = any("flagd" in i.get("description", "").lower() for i in issues)
        has_db_gap = any("db." in i.get("description", "") for i in issues)
        if has_flagd or has_db_gap:
            return "approve", "accurately identifies flagd errors and/or missing db.* attributes"
        return "approve", "db analysis consistent with environment state"

    if domain == "performance":
        # Clean environment — performance specialist should report no issues
        return "approve", "performance findings consistent (no K8s profiling expected in Docker Compose)"

    return "approve", "no issues detected with analysis"


# ── Main loop ─────────────────────────────────────────────────────────────────

def process_run(run_id, labeled_index):
    detail = get_detail(run_id)
    if not detail:
        print(f"  [skip] could not fetch detail")
        return 0

    specialists = detail.get("specialists", {})
    if not specialists:
        print(f"  [skip] no specialist data in assessment")
        return 0

    pending_domains = [
        d for d in DOMAINS
        if f"{run_id}|{d}" not in labeled_index and specialists.get(d)
    ]
    if not pending_domains:
        return 0

    galileo_decisions = {}
    engine_tag = "rules"
    if EVAL_ENGINE == "galileo":
        try:
            from tools import galileo_eval
            galileo_decisions = galileo_eval.evaluate_run(
                run_id, specialists, ENV_FACTS, pending_domains
            )
            engine_tag = "galileo"
        except Exception as e:
            print(f"  [warn] Galileo eval failed ({e}) — falling back to rule-based evaluate()")

    labeled = 0
    for domain in pending_domains:
        spec = specialists[domain]
        if domain in galileo_decisions:
            decision, reason, scores = galileo_decisions[domain]
            log_galileo_score(run_id, domain, decision, reason, scores)
        else:
            decision, reason = evaluate(domain, spec, detail)
        post_label(run_id, domain, decision)
        marker = "✓" if decision == "approve" else "✗"
        print(f"    {marker} {domain:16s} [{decision}] ({engine_tag if domain in galileo_decisions else 'rules'})  {reason}")
        labeled += 1

    return labeled


def main():
    parser = argparse.ArgumentParser(description="Auto-label o11y-agent assessments")
    parser.add_argument("--once", action="store_true",
                        help="Label all current runs then exit (no polling loop)")
    args = parser.parse_args()

    print("=" * 65)
    print("  o11y-agent assessment auto-labeler")
    print(f"  Agent:      {AGENT_URL}")
    print(f"  Supervisor: {SUPERVISOR_URL}")
    print(f"  Eval:       {EVAL_ENGINE}")
    print(f"  Mode:       {'single pass' if args.once else 'continuous (poll every 60s)'}")
    print("=" * 65)
    print()

    while True:
        try:
            labeled_index = get_labeled_index()
            history       = get_history()

            newly_labeled = 0
            for run in history:
                run_id = run.get("run_id")
                if not run_id:
                    continue
                # Check if any domain in this run still needs a label
                needs_label = any(
                    f"{run_id}|{d}" not in labeled_index
                    for d in DOMAINS
                )
                if not needs_label:
                    continue

                ts = run.get("timestamp", "")[:16].replace("T", " ")
                score = run.get("instrumentation_score")
                score_str = f"  score={score}" if score is not None else ""
                print(f"[{run_id}]  {ts}{score_str}")
                n = process_run(run_id, labeled_index)
                newly_labeled += n
                if n:
                    print()

            if newly_labeled == 0:
                print(f"  all {len(history)} runs fully labeled — waiting for next assessment...")
            else:
                total = len(labeled_index) + newly_labeled
                print(f"  labeled {newly_labeled} new examples  (total: {total})")

        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[error] {e}")

        if args.once:
            break

        print()
        time.sleep(60)


if __name__ == "__main__":
    main()
