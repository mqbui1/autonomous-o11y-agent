#!/usr/bin/env bash
# Full 17-scenario fault-injection matrix, run against BOTH Bedrock and the
# local o11y-agent-14b (via a GPU-backed Ollama instance on a RunPod pod, to
# test whether GPU inference closes the reliability gap found on CPU) for
# each scenario, to verify RCA/synthesis quality parity fault-mode by
# fault-mode -- not just absence of crashes/timeouts. Adapted from
# run_scenario_batches.sh's flag set/revert pattern. Every flag's baseline is
# "off"; always reverted via trap, since demo.flagd.json is live shared
# config for the running astroshop-local demo.
#
# Bedrock and the 14b run CONCURRENTLY per scenario (independent backends --
# AWS cloud vs. RunPod GPU pod -- so they don't contend with each other),
# roughly halving wall-clock time vs. sequential.
#
# AWS creds: refreshed once at start via deploy/refresh-aws-creds.sh, then
# re-refreshed every 30 min in the background for the duration of this run.
# IMPORTANT (confirmed 2026-09-13): refresh-aws-creds.sh's credential-process
# only mints a genuinely NEW token if the underlying Okta session is fresh --
# if that session is stale, every "refresh" silently returns the IDENTICAL
# (soon-to-expire) token for hours, giving false reassurance while every
# Bedrock call after the real ~1hr expiry fails with ExpiredTokenException.
# A prior session saw a ~12h window; this one expired after only ~1h14m --
# it is NOT reliable, don't assume a long window. **Run `dev-login aws --force`
# yourself right before starting this script** for a guaranteed-fresh session
# on any run expected to run longer than ~1hr. The loop below also
# self-detects a no-op refresh (identical expiry twice in a row) and the
# per-scenario loop aborts fast on a real credential failure -- see
# check_fatal_errors() -- rather than silently limping through all 17
# scenarios on a dead Bedrock pipeline.
set -uo pipefail
cd "$(dirname "$0")/.."

FLAGD_FILE="deploy/demo.flagd.json"
# Separate log dir for the 2026-09-13 SPECIALIST_MAX_CONCURRENCY=7 re-test
# (7 confirmed as the true sweet spot via a full 1/4/5/6/7/8 sweep -- same
# per-request latency as 4 but ~72% more aggregate throughput; 4 was an
# earlier, incomplete finding) -- keeps this run's logs from clobbering
# prior runs' logs, needed for a fair before/after comparison.
LOG_DIR="/tmp/parity_comparison_concurrency7"
mkdir -p "$LOG_DIR"

DEDICATED_OLLAMA_URL="http://194.68.245.56:22033/v1"
FT_MODEL="o11y-agent-14b"

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
}
trap revert_all EXIT

# Confirmed 2026-09-13: a specialist calling submit_findings still lets the
# coordinator synthesize a full "AUTONOMOUS O11Y AGENT ASSESSMENT" banner+
# report even when EVERY specialist failed (e.g. all 10 hit the same AWS
# credential error) -- so banner presence alone (the only check this script
# used to have) cannot detect this failure mode. Grep the log directly for
# the actual error signature instead.
check_fatal_errors() {
  local f="$1" label="$2"
  if grep -qE "ExpiredTokenException|NoCredentialsError|CredentialRetrievalError|InvalidClientTokenId" "$f"; then
    echo "  *** FATAL: $label hit an AWS credential error -- see $f ***"
    return 1
  fi
  return 0
}

CREDS_STALE_MARKER="$LOG_DIR/.creds_stale"
rm -f "$CREDS_STALE_MARKER"

# Background AWS credential refresh every 30 min for the duration of this run.
# Detects a no-op refresh (identical "Valid until" twice in a row -- see the
# top-of-file comment) and writes CREDS_STALE_MARKER so the main per-scenario
# loop below can warn immediately instead of only surfacing this at the end.
(
  last_expiry=""
  while true; do
    sleep 1800
    out=$(bash deploy/refresh-aws-creds.sh 2>&1)
    echo "$out" >> "$LOG_DIR/creds_refresh.log"
    new_expiry=$(echo "$out" | grep -oE 'Valid until: [^[:space:]]+' | head -1)
    if [[ -n "$last_expiry" && "$new_expiry" == "$last_expiry" ]]; then
      echo "*** WARNING $(date): refresh returned IDENTICAL expiry ($new_expiry) -- Okta session is stale, this refresh was a no-op. Run 'dev-login aws --force' now. ***" | tee -a "$LOG_DIR/creds_refresh.log" > "$CREDS_STALE_MARKER"
    fi
    last_expiry="$new_expiry"
  done
) &
refresh_pid=$!
trap 'kill "$refresh_pid" >/dev/null 2>&1 || true; revert_all' EXIT

run_provider() {
  local provider="$1" cname="$2" outfile="$3"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  if [[ "$provider" == "bedrock" ]]; then
    (cd deploy && docker compose run -d --no-deps --name "$cname" \
      -e LLM_PROVIDER=bedrock \
      o11y-agent python3 main.py --environment astroshop-local --verbose \
      > "${outfile}.launch.log" 2>&1)
  else
    # SPECIALIST_MAX_CONCURRENCY=7 matches the RunPod A6000's
    # OLLAMA_NUM_PARALLEL=7 (validated 2026-09-13 via a full 1/4/5/6/7/8
    # sweep: 7 is the true ceiling -- same per-request latency as 4 but
    # ~72% more aggregate throughput; 8 regresses on both metrics as Ollama
    # spins up a second llama-server process instead of batching within one).
    (cd deploy && docker compose run -d --no-deps --name "$cname" \
      -e LLM_PROVIDER=openai -e OPENAI_BASE_URL="$DEDICATED_OLLAMA_URL" \
      -e OPENAI_API_KEY=none -e OPENAI_MODEL="$FT_MODEL" \
      -e SPECIALIST_MAX_CONCURRENCY=7 \
      o11y-agent python3 main.py --environment astroshop-local --verbose \
      > "${outfile}.launch.log" 2>&1)
  fi
}

wait_and_collect() {
  local cname="$1" outfile="$2"
  # 2400s (40min) watchdog -- generous margin above the observed 16-28min
  # full-run time for o11y-agent-14b (all 10 specialists); 900s was too
  # tight and killed a real in-progress run mid-flight (confirmed 2026-09-06).
  ( sleep 2400 && docker kill "$cname" >/dev/null 2>&1 ) &
  local watchdog_pid=$!
  docker wait "$cname" > /dev/null 2>&1
  kill "$watchdog_pid" >/dev/null 2>&1 || true
  docker logs "$cname" > "$outfile" 2>&1
  docker rm -f "$cname" >/dev/null 2>&1 || true
}

consecutive_bedrock_failures=0

for entry in "${SCENARIOS[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  echo "=== Scenario: $name ==="

  if [[ -f "$CREDS_STALE_MARKER" ]]; then
    echo "  *** $(cat "$CREDS_STALE_MARKER") ***"
  fi

  if [[ "$rest" == *"="* ]]; then
    IFS=',' read -ra pairs <<< "$rest"
    for p in "${pairs[@]}"; do
      f="${p%%=*}"; v="${p##*=}"
      echo "  set $f -> $v"
      set_flag "$f" "$v"
    done
  else
    f="${rest%%:*}"; v="${rest##*:}"
    echo "  set $f -> $v"
    set_flag "$f" "$v"
  fi

  echo "  waiting 75s for flagd hot-reload + traffic to reflect fault..."
  sleep 75

  bedrock_cname="o11y-parity-bedrock-${name}"
  ft_cname="o11y-parity-14b-${name}"
  bedrock_out="$LOG_DIR/${name}.bedrock.log"
  ft_out="$LOG_DIR/${name}.14b.log"

  echo "  launching Bedrock ($bedrock_cname) and 14b ($ft_cname) concurrently"
  run_provider bedrock "$bedrock_cname" "$LOG_DIR/${name}.bedrock"
  run_provider openai "$ft_cname" "$LOG_DIR/${name}.14b"

  wait_and_collect "$bedrock_cname" "$bedrock_out" &
  bedrock_wait_pid=$!
  wait_and_collect "$ft_cname" "$ft_out" &
  ft_wait_pid=$!
  wait "$bedrock_wait_pid"
  wait "$ft_wait_pid"

  for f in "$bedrock_out" "$ft_out"; do
    if [[ ! -s "$f" ]] || ! grep -q "AUTONOMOUS O11Y AGENT ASSESSMENT" "$f"; then
      echo "  *** WARNING: $f missing expected report banner -- check log ***"
    fi
  done

  if check_fatal_errors "$bedrock_out" "Bedrock/$name"; then
    consecutive_bedrock_failures=0
  else
    consecutive_bedrock_failures=$((consecutive_bedrock_failures + 1))
    if [[ "$consecutive_bedrock_failures" -ge 2 ]]; then
      echo "  *** ABORTING: 2 consecutive Bedrock credential failures -- the AWS session is dead, not a fluke. Run 'dev-login aws --force' then re-run (see training/retry_failed_scenarios.sh pattern to only redo the affected scenarios). ***"
      exit 1
    fi
  fi
  check_fatal_errors "$ft_out" "14b/$name" || true  # non-AWS backend; log only, don't count toward the abort threshold

  echo "  reverting flags for $name"
  apply_pairs "$rest" "off"
  echo "=== Done: $name ==="
done

trap - EXIT
kill "$refresh_pid" >/dev/null 2>&1 || true
echo "All 17 scenarios complete. Logs in $LOG_DIR/<scenario>.{bedrock,14b}.log"
