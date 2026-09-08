#!/usr/bin/env bash
# Full 17-scenario fault-injection matrix, run against BOTH Bedrock and the
# local o11y-agent-14b (via a dedicated Ollama instance on :11435 to avoid
# shared-instance contention) for each scenario, to verify RCA/synthesis
# quality parity fault-mode by fault-mode -- not just absence of
# crashes/timeouts. Adapted from run_scenario_batches.sh's flag set/revert
# pattern. Every flag's baseline is "off"; always reverted via trap, since
# demo.flagd.json is live shared config for the running astroshop-local demo.
#
# Bedrock and the 14b run CONCURRENTLY per scenario (independent backends --
# AWS cloud vs. local dedicated Ollama port -- so they don't contend with
# each other), roughly halving wall-clock time vs. sequential.
#
# AWS creds: refreshed once at start via deploy/refresh-aws-creds.sh, then
# re-refreshed every 30 min in the background for the duration of this run
# (dev-login credential-process works non-interactively as long as the
# underlying Okta session is still alive -- confirmed 2026-09-06, one refresh
# yielded a ~12h token window, well beyond the 1hr figure previously assumed).
set -uo pipefail
cd "$(dirname "$0")/.."

FLAGD_FILE="deploy/demo.flagd.json"
LOG_DIR="/tmp/parity_comparison"
mkdir -p "$LOG_DIR"

DEDICATED_OLLAMA_URL="http://host.docker.internal:11435/v1"
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

# Background AWS credential refresh every 30 min for the duration of this run.
(
  while true; do
    sleep 1800
    bash deploy/refresh-aws-creds.sh >> "$LOG_DIR/creds_refresh.log" 2>&1
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
    (cd deploy && docker compose run -d --no-deps --name "$cname" \
      -e LLM_PROVIDER=openai -e OPENAI_BASE_URL="$DEDICATED_OLLAMA_URL" \
      -e OPENAI_API_KEY=none -e OPENAI_MODEL="$FT_MODEL" \
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

for entry in "${SCENARIOS[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  echo "=== Scenario: $name ==="

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

  echo "  reverting flags for $name"
  apply_pairs "$rest" "off"
  echo "=== Done: $name ==="
done

trap - EXIT
kill "$refresh_pid" >/dev/null 2>&1 || true
echo "All 17 scenarios complete. Logs in $LOG_DIR/<scenario>.{bedrock,14b}.log"
