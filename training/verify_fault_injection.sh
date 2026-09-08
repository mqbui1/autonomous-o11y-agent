#!/usr/bin/env bash
# Ground-truth gate for the 17-scenario fault-injection matrix: for each
# scenario, confirm the target service's telemetry ACTUALLY MOVES when the
# flagd flag is toggled, independent of any specialist/LLM output. Run this
# BEFORE trusting fault-mode-by-fault-mode RCA grading from
# run_parity_comparison.sh — motivated by a confirmed confound (2026-09-06)
# where both Bedrock and o11y-agent-14b reported near-identical numbers
# across unrelated scenarios, which is indistinguishable from "the agent
# can't tell faults apart" unless we first rule out "the fault never showed
# up in telemetry at all" (target service silent, load-generator not routing
# traffic to it, or flagd hot-reload not actually taking effect).
#
# For each scenario: revert to baseline, settle, snapshot target service(s),
# toggle the flag, wait for propagation (same 75s window
# run_parity_comparison.sh uses), snapshot again, revert, and diff.
#
# Verdict per scenario:
#   OBSERVED — at least one target signal moved beyond its threshold (or
#              telemetry appeared where there was none) — this scenario is
#              eligible for real fault-specific RCA grading.
#   FLAT     — data was present both before and after, but didn't move
#              enough to attribute to the fault — investigate before
#              grading (borderline; may need a bigger flag variant/longer
#              wait, not necessarily a code bug).
#   NO_DATA  — the target service reported zero telemetry both before and
#              after — this IS the confound: grading RCA quality against
#              this scenario is not meaningful until instrumentation/load
#              routing is fixed.
# Optional: comma-separated list of scenario names to run (default: all).
# Useful for re-verifying a specific fix without a full 17-scenario re-run,
# e.g. `bash verify_fault_injection.sh paymentFailure100,paymentUnreachable`.
ONLY="${1:-}"

set -uo pipefail
cd "$(dirname "$0")/.."

FLAGD_FILE="deploy/demo.flagd.json"
LOG_DIR="/tmp/fault_injection_verify"
mkdir -p "$LOG_DIR"

CONTAINER="o11y-verify"
ENVIRONMENT="astroshop-local"
SETTLE_SECONDS=30   # after reverting, before snapshotting baseline
PROPAGATE_SECONDS=75  # same wait run_parity_comparison.sh uses post-toggle

# name:flagspec (identical format/semantics to run_parity_comparison.sh)
SCENARIOS=(
  "productCatalogFailure:productCatalogFailure:on"
  "recommendationCacheFailure:recommendationCacheFailure:on"
  "adManualGc:adManualGc:on"
  "adHighCpu:adHighCpu:on"
  "adFailure:adFailure:on"
  "kafkaQueueProblems:kafkaQueueProblems:on"
  "cartFailure100:cartFailure:100%"
  "paymentFailure100:paymentFailure:100%"
  "paymentUnreachable:paymentUnreachable:on"
  "imageSlowLoad10sec:imageSlowLoad:10sec"
  "failedReadinessProbe:failedReadinessProbe:on"
  "emailMemoryLeak1000x:emailMemoryLeak:1000x"
  "intlShippingSlowdown10sec:intlShippingSlowdown:10sec"
  "loadGeneratorFloodHomepage:loadGeneratorFloodHomepage:on"
  "compound_checkout:cartFailure=50%,paymentFailure=50%"
  "compound_ad_pressure:adHighCpu=on,adManualGc=on"
  "compound_backend:kafkaQueueProblems=on,recommendationCacheFailure=on"
)

# name:service:signal,service:signal,... — the checks to run per scenario.
# Mapped from each flag's own description in demo.flagd.json. Where a fault's
# expected signal type is ambiguous (e.g. a cache failure could show up as
# either errors or latency), multiple signals are checked and the scenario
# passes if ANY of them moves. Plain indexed array (not `declare -A`) — the
# macOS system /bin/bash is 3.2, which has no associative-array support;
# looked up via get_checks() below, same style as run_parity_comparison.sh's
# SCENARIOS parsing.
CHECKS=(
  "productCatalogFailure:product-catalog:error_rate"
  "recommendationCacheFailure:recommendation:error_rate,recommendation:latency"
  "adManualGc:ad:latency,ad:infra_cpu"
  "adHighCpu:ad:infra_cpu"
  "adFailure:ad:error_rate"
  "kafkaQueueProblems:kafka:latency"
  "cartFailure100:cart:error_rate"
  "paymentFailure100:payment:error_rate"
  "paymentUnreachable:payment:error_rate"
  "imageSlowLoad10sec:frontend-proxy:latency"
  "failedReadinessProbe:cart:error_rate,cart:latency"
  "emailMemoryLeak1000x:email:infra_mem"
  "intlShippingSlowdown10sec:shipping:latency"
  "loadGeneratorFloodHomepage:frontend-proxy:latency,frontend-proxy:error_rate"
  "compound_checkout:cart:error_rate,payment:error_rate"
  "compound_ad_pressure:ad:infra_cpu,ad:latency"
  "compound_backend:kafka:latency,recommendation:error_rate"
)

get_checks() {
  local name="$1"
  for entry in "${CHECKS[@]}"; do
    if [[ "$entry" == "$name:"* ]]; then
      echo "${entry#*:}"
      return 0
    fi
  done
  return 1
}

# Per-scenario override for PROPAGATE_SECONDS (and the matching post-toggle
# --minutes window). Confirmed 2026-09-07: paymentFailure/paymentUnreachable
# are NOT structurally invisible to error_rate (a clean manual re-test with a
# longer wait showed real 24-55% error rates on payment — error_rate DOES
# capture the fault when it fires). The demo.user_context.loyalty_level=gold
# attribute on the resulting error log suggests this fault is cohort-targeted
# (only affects a subset of synthetic users, not blanket 100% of traffic), so
# the default 75s window has real per-run variance in whether it samples an
# affected transaction at all -- explained the earlier FLAT verdict, which was
# a sampling-luck artifact, not a signal-mapping bug. Widen the window for
# just these two scenarios rather than globally, to keep the matrix's total
# runtime increase scoped to the scenarios that actually need it.
PROPAGATE_OVERRIDE=(
  "paymentFailure100:180"
  "paymentUnreachable:180"
)

get_propagate_seconds() {
  local name="$1"
  for entry in "${PROPAGATE_OVERRIDE[@]}"; do
    if [[ "$entry" == "$name:"* ]]; then
      echo "${entry#*:}"
      return 0
    fi
  done
  echo "$PROPAGATE_SECONDS"
}

set_flag() {
  local flag="$1" variant="$2"
  python3 - "$FLAGD_FILE" "$flag" "$variant" <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
}

apply_pairs() {
  local rest="$1" value_side="$2"  # value_side: "target" (v from pair) or "off"
  if [[ "$rest" == *"="* ]]; then
    IFS=',' read -ra pairs <<< "$rest"
    for p in "${pairs[@]}"; do
      f="${p%%=*}"
      if [[ "$value_side" == "off" ]]; then
        set_flag "$f" "off"
      else
        set_flag "$f" "${p##*=}"
      fi
    done
  else
    f="${rest%%:*}"
    if [[ "$value_side" == "off" ]]; then
      set_flag "$f" "off"
    else
      set_flag "$f" "${rest##*:}"
    fi
  fi
}

revert_all() {
  echo "[revert] restoring all fault flags to off baseline"
  for entry in "${SCENARIOS[@]}"; do
    apply_pairs "${entry#*:}" "off"
  done
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap revert_all EXIT

# Isolated sleeping container (not the persistent --watch agent) purely to
# reuse the o11y-agent image's tools/ package + SPLUNK_REALM/SPLUNK_ACCESS_TOKEN
# env vars for direct SignalFlow queries -- same pattern as the hot-patch
# validation workflow in run_parity_comparison.sh.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
(cd deploy && docker compose run -d --rm --no-deps --name "$CONTAINER" --entrypoint sleep o11y-agent infinity >/dev/null)

# Copy the snapshot tool into the running container once (bind-mounted tools/
# don't include this script, and it needs to run in-container to reuse the
# container's SPLUNK_REALM/SPLUNK_ACCESS_TOKEN env vars + tools/ package).
# Must land in /opt/agent, not /tmp -- python puts a script's OWN directory
# first on sys.path when run as a file (not cwd), so /tmp would shadow the
# real `tools` package with nothing importable. -w /opt/agent on exec below
# is belt-and-suspenders for the same reason.
docker cp training/verify_fault_injection.py "$CONTAINER:/opt/agent/verify_fault_injection.py"

snapshot() {
  local checks="$1" outfile="$2" minutes="${3:-3}"
  local args=(--environment "$ENVIRONMENT" --minutes "$minutes")
  IFS=',' read -ra pairs <<< "$checks"
  for p in "${pairs[@]}"; do
    args+=(--check "$p")
  done
  docker exec -w /opt/agent "$CONTAINER" python3 verify_fault_injection.py "${args[@]}" > "$outfile"
}

echo "scenario,verdict,detail" > "$LOG_DIR/summary.csv"

for entry in "${SCENARIOS[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"

  if [[ -n "$ONLY" ]]; then
    match=0
    IFS=',' read -ra only_names <<< "$ONLY"
    for on in "${only_names[@]}"; do
      [[ "$on" == "$name" ]] && match=1
    done
    [[ "$match" == "0" ]] && continue
  fi

  checks="$(get_checks "$name")"
  echo "=== Scenario: $name ==="
  if [[ -z "$checks" ]]; then
    echo "  *** no CHECKS mapping for $name — skipping ***"
    continue
  fi

  echo "  reverting to baseline + settling ${SETTLE_SECONDS}s"
  apply_pairs "$rest" "off"
  sleep "$SETTLE_SECONDS"

  baseline_out="$LOG_DIR/${name}.baseline.json"
  post_out="$LOG_DIR/${name}.post.json"
  diff_out="$LOG_DIR/${name}.diff.json"

  echo "  snapshotting baseline ($checks)"
  snapshot "$checks" "$baseline_out"

  echo "  toggling flag(s): $rest"
  if [[ "$rest" == *"="* ]]; then
    IFS=',' read -ra pairs <<< "$rest"
    for p in "${pairs[@]}"; do
      f="${p%%=*}"; v="${p##*=}"
      set_flag "$f" "$v"
    done
  else
    f="${rest%%:*}"; v="${rest##*:}"
    set_flag "$f" "$v"
  fi

  wait_secs="$(get_propagate_seconds "$name")"
  # Post-toggle window must fully cover the propagate wait (else it's flat
  # noise) but not extend so far past it that pre-toggle baseline traffic
  # dilutes the result -- default 3min already does this reasonably for the
  # default 75s wait, so only recompute when overridden.
  if [[ "$wait_secs" == "$PROPAGATE_SECONDS" ]]; then
    post_minutes=3
  else
    post_minutes="$(python3 -c "print(round($wait_secs/60 + 0.5, 2))")"
  fi

  echo "  waiting ${wait_secs}s for flagd hot-reload + traffic to reflect fault..."
  sleep "$wait_secs"

  echo "  snapshotting post-toggle (minutes=$post_minutes)"
  snapshot "$checks" "$post_out" "$post_minutes"

  echo "  reverting flags for $name"
  apply_pairs "$rest" "off"

  python3 training/verify_fault_injection.py --diff "$baseline_out" "$post_out" > "$diff_out"
  verdict=$(python3 -c "import json; print(json.load(open('$diff_out'))['scenario_verdict'])")
  echo "  verdict: $verdict"
  echo "$name,$verdict,$diff_out" >> "$LOG_DIR/summary.csv"
  echo "=== Done: $name ==="
done

trap - EXIT
echo
echo "All scenarios checked. Summary: $LOG_DIR/summary.csv"
column -t -s, "$LOG_DIR/summary.csv"
