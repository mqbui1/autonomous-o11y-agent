#!/usr/bin/env python3
"""
Standalone ground-truth telemetry snapshot tool — bypasses the agent entirely.

Queries a short lookback window directly via the same SignalFlow-backed helpers
the RCA specialist uses (tools/rca_tools.py get_service_error_rate/
get_service_latency/get_infra_metrics), so a scenario's target service(s) can be
snapshotted BEFORE and AFTER a flagd fault toggle. Used by
verify_fault_injection.sh to gate whether a fault-injection scenario actually
produced an observable telemetry change before any specialist-quality grading
is attempted against it.

Run inside the o11y-agent container (has SPLUNK_REALM/SPLUNK_ACCESS_TOKEN env
vars and the tools/ package already available):
    docker compose exec -T o11y-agent python3 /tmp/verify_fault_injection.py \\
        --environment astroshop-local --minutes 3 \\
        --check product-catalog:error_rate --check ad:infra_cpu

Diff mode (run standalone, no Splunk creds needed — just compares two prior
snapshot JSON files):
    python3 verify_fault_injection.py --diff baseline.json post.json
"""
import argparse
import json
import os
import sys

# Per-signal-type minimum change required to call a scenario's fault
# "observed" rather than "flat" — deliberately generous thresholds (well
# above normal noise in this environment) since the goal is catching a clear
# signal, not precise sensitivity tuning.
#
# error_rate lowered 3.0 -> 1.0pp on 2026-09-07: the original 3.0pp bar was
# calibrated before two confounds were found (otel-collector memory_limiter
# silently dropping data; get_service_latency querying a nonexistent metric
# name). After fixing both, re-running the matrix showed confirmed *real*
# error-rate movement from actual fault toggles sitting at 1.4-2.4pp
# (productCatalogFailure 0.0->1.4%, cartFailure100 0.0->2.37%) — diluted below
# 3.0pp because the toggled fault only affects one RPC type or one product
# out of a service's total traffic, not because the fault didn't fire.
_THRESHOLDS = {
    "error_rate": ("absolute", 1.0),     # +1 percentage point
    "latency": ("absolute", 50.0),       # +50ms p99 peak
    "infra_cpu": ("absolute", 15.0),     # +15 percentage points peak CPU
    "infra_mem": ("relative", 0.2),      # +20% peak memory
}


def _run_check(service: str, signal: str, environment: str, hours: float) -> dict:
    import tools.rca_tools as rca_tools

    if signal == "error_rate":
        raw = json.loads(rca_tools.get_service_error_rate(service=service, environment=environment, hours=hours))
        no_data = raw.get("total_requests", 0) == 0
        value = raw.get("error_rate_pct", 0)
    elif signal == "latency":
        raw = json.loads(rca_tools.get_service_latency(service=service, environment=environment, hours=hours))
        no_data = "note" in raw  # helper's own explicit no-data marker
        value = raw.get("p99_peak_ms")
    elif signal in ("infra_cpu", "infra_mem"):
        # get_infra_metrics rolls up on a 5-minute mean internally — a shorter
        # lookback window than that returns zero data points regardless of
        # whether the fault fired. Force a minimum window for these two signal
        # types only; error_rate/latency use 1-minute resolution and stay
        # tightly scoped to whatever window the caller requested.
        raw = json.loads(rca_tools.get_infra_metrics(environment=environment, service=service, hours=max(hours, 8 / 60)))
        metrics = raw.get("metrics", {})
        no_data = not metrics
        if signal == "infra_cpu":
            block = metrics.get("k8s_cpu") or metrics.get("host_cpu_pct") or {}
            value = block.get("peak_pct")
        else:
            block = metrics.get("k8s_memory_mb")
            if block:
                value = block.get("peak_mb")
            else:
                # Host-level fallback (non-k8s environments) reports a
                # percentage, not MB — same peak_pct shape as host_cpu_pct.
                block = metrics.get("host_mem_pct") or {}
                value = block.get("peak_pct")
    else:
        raise ValueError(f"Unknown signal type: {signal}")

    return {"service": service, "signal": signal, "no_data": no_data, "value": value, "raw": raw}


def _snapshot(args) -> None:
    import tools._runner as _runner
    from config import AgentConfig

    _runner._config = AgentConfig(
        realm=os.environ["SPLUNK_REALM"],
        token=os.environ["SPLUNK_ACCESS_TOKEN"],
        environment=args.environment,
    )

    results = []
    for check in args.check:
        service, signal = check.split(":", 1)
        try:
            results.append(_run_check(service, signal, args.environment, args.minutes / 60.0))
        except Exception as exc:
            results.append({"service": service, "signal": signal, "error": str(exc)})

    json.dump(results, sys.stdout, indent=2)


def _verdict_for_pair(before: dict, after: dict) -> str:
    """Compare one (service, signal) check across baseline/post snapshots."""
    if "error" in before or "error" in after:
        return "ERROR"
    if before.get("no_data") and after.get("no_data"):
        return "NO_DATA"
    b_val = before.get("value") or 0
    a_val = after.get("value") or 0
    kind, threshold = _THRESHOLDS[before["signal"]]
    if kind == "absolute":
        moved = (a_val - b_val) >= threshold
    else:
        moved = (a_val - b_val) >= threshold * max(b_val, 1)
    # Data appearing where there was none before is itself a clear signal,
    # regardless of the magnitude thresholds above.
    if before.get("no_data") and not after.get("no_data"):
        moved = True
    return "OBSERVED" if moved else "FLAT"


def _diff(baseline_path: str, post_path: str) -> None:
    with open(baseline_path) as f:
        before_list = json.load(f)
    with open(post_path) as f:
        after_list = json.load(f)
    after_by_key = {(r["service"], r["signal"]): r for r in after_list}

    verdicts = []
    for before in before_list:
        key = (before["service"], before["signal"])
        after = after_by_key.get(key, {"error": "missing from post snapshot"})
        verdicts.append({
            "service": before["service"],
            "signal": before["signal"],
            "before": before.get("value"),
            "after": after.get("value"),
            "verdict": _verdict_for_pair(before, after),
        })

    scenario_verdict = "OBSERVED" if any(v["verdict"] == "OBSERVED" for v in verdicts) else (
        "NO_DATA" if all(v["verdict"] == "NO_DATA" for v in verdicts) else "FLAT"
    )
    json.dump({"checks": verdicts, "scenario_verdict": scenario_verdict}, sys.stdout, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", help="Required for snapshot mode")
    parser.add_argument("--minutes", type=float, default=3.0, help="Lookback window in minutes")
    parser.add_argument("--check", action="append", metavar="SERVICE:SIGNAL",
                         help="Repeatable. SIGNAL is one of error_rate, latency, infra_cpu, infra_mem.")
    parser.add_argument("--diff", nargs=2, metavar=("BASELINE_JSON", "POST_JSON"),
                         help="Diff mode: compare two prior snapshot files instead of querying live data.")
    args = parser.parse_args()

    if args.diff:
        _diff(*args.diff)
        return

    if not args.environment or not args.check:
        parser.error("--environment and at least one --check are required in snapshot mode")
    _snapshot(args)


if __name__ == "__main__":
    main()
