#!/usr/bin/env bash
# Retry the scenarios from run_scenario_batches.sh that produced zero new
# captures because AWS Bedrock creds expired mid-run (confirmed 2026-08-16,
# ~01:02 PDT) or because of the cold-start hiccup right after the Docker
# Desktop restart (productCatalogFailure). Same set/revert logic as the
# parent script, just a smaller SCENARIOS list.
set -uo pipefail
cd "$(dirname "$0")/.."

FLAGD_FILE="deploy/demo.flagd.json"
LOG_DIR="/tmp/scenario_batches"
mkdir -p "$LOG_DIR"

SCENARIOS=(
  "productCatalogFailure:productCatalogFailure:on"
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

  cname="o11y-teacher-retry-${name}"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  pre_count=$(docker run --rm -v deploy_agent-state:/data alpine sh -c "find /data/training -iname '*.jsonl' 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  echo "  launching isolated Bedrock capture container $cname (pre_count=$pre_count)"
  (cd deploy && docker compose run -d --no-deps --name "$cname" \
    -e LLM_PROVIDER=bedrock -e CAPTURE_TRAINING_DATA=true \
    o11y-agent python3 main.py --environment astroshop-local --verbose \
    > "$LOG_DIR/${name}.retry.launch.log" 2>&1)

  ( sleep 600 && docker kill "$cname" >/dev/null 2>&1 ) &
  watchdog_pid=$!
  docker wait "$cname" > /dev/null 2>&1
  kill "$watchdog_pid" >/dev/null 2>&1 || true

  docker logs "$cname" > "$LOG_DIR/${name}.retry.log" 2>&1
  docker rm -f "$cname" >/dev/null 2>&1 || true

  post_count=$(docker run --rm -v deploy_agent-state:/data alpine sh -c "find /data/training -iname '*.jsonl' 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  new_captures=$((post_count - pre_count))
  echo "  post_count=$post_count new_captures=$new_captures"
  if [[ "$new_captures" -le 0 ]]; then
    echo "  *** WARNING: scenario $name produced ZERO new captures -- check $LOG_DIR/${name}.retry.log ***"
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
echo "All retry scenarios complete."
