#!/usr/bin/env bash
# Toggle each demo.flagd.json fault flag on, run one isolated Bedrock teacher
# capture batch (all 10 specialists) against astroshop-local, then revert the
# flag to "off" before moving to the next scenario. Every flag's baseline in
# this file is "off" -- this script always restores that baseline, even on
# failure (trap), since demo.flagd.json is live shared config for the running
# astroshop-local demo/dashboards.
set -uo pipefail
cd "$(dirname "$0")/.."

FLAGD_FILE="deploy/demo.flagd.json"
LOG_DIR="/tmp/scenario_batches"
mkdir -p "$LOG_DIR"

# name:flag:variant  (or name:flag1=v1,flag2=v2 for compound scenarios)
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

revert_all() {
  echo "[revert] restoring all fault flags to off baseline"
  for entry in "${SCENARIOS[@]}"; do
    rest="${entry#*:}"
    if [[ "$rest" == *"="* ]]; then
      IFS=',' read -ra pairs <<< "$rest"
      for p in "${pairs[@]}"; do
        f="${p%%=*}"
        set_flag "$f" "off"
      done
    else
      f="${rest%%:*}"
      set_flag "$f" "off"
    fi
  done
}
trap revert_all EXIT

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

  cname="o11y-teacher-scn-${name}"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  pre_count=$(docker run --rm -v deploy_agent-state:/data alpine sh -c "find /data/training -iname '*.jsonl' 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  echo "  launching isolated Bedrock capture container $cname (pre_count=$pre_count)"
  # No --rm here (deliberate): we need the container to still exist after exit
  # so `docker logs` can retrieve real output before cleanup. `docker compose
  # run -d --rm` auto-removes as soon as the process exits, which raced with
  # the old poll-loop + docker logs below and silently lost all diagnostic
  # output for a run that (confirmed 2026-08-15) wrote zero new captures.
  (cd deploy && docker compose run -d --no-deps --name "$cname" \
    -e LLM_PROVIDER=bedrock -e CAPTURE_TRAINING_DATA=true \
    o11y-agent python3 main.py --environment astroshop-local --verbose \
    > "$LOG_DIR/${name}.launch.log" 2>&1)

  # docker wait blocks efficiently until the container exits (no polling
  # overhead, no race with auto-removal). 10 min safety timeout via `timeout`
  # is unavailable on stock macOS, so bound it manually with a background kill.
  ( sleep 600 && docker kill "$cname" >/dev/null 2>&1 ) &
  watchdog_pid=$!
  docker wait "$cname" > /dev/null 2>&1
  kill "$watchdog_pid" >/dev/null 2>&1 || true

  docker logs "$cname" > "$LOG_DIR/${name}.log" 2>&1
  docker rm -f "$cname" >/dev/null 2>&1 || true

  post_count=$(docker run --rm -v deploy_agent-state:/data alpine sh -c "find /data/training -iname '*.jsonl' 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  new_captures=$((post_count - pre_count))
  echo "  post_count=$post_count new_captures=$new_captures"
  if [[ "$new_captures" -le 0 ]]; then
    echo "  *** WARNING: scenario $name produced ZERO new captures -- check $LOG_DIR/${name}.log ***"
  fi

  echo "  reverting flags for $name"
  if [[ "$rest" == *"="* ]]; then
    IFS=',' read -ra pairs <<< "$rest"
    for p in "${pairs[@]}"; do
      f="${p%%=*}"
      set_flag "$f" "off"
    done
  else
    f="${rest%%:*}"
    set_flag "$f" "off"
  fi
  echo "=== Done: $name ==="
done

trap - EXIT
echo "All scenarios complete."
