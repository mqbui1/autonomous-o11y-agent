#!/usr/bin/env bash
# demo-degrade.sh — Put the environment into a "bad instrumentation" state for demo Act 1.
#
# What this does:
#   1. Swaps in the degraded OTel Collector config (missing host.name stamping,
#      missing deployment.environment span tag, missing sf_environment)
#   2. Stops 4 services to simulate silent / uninstrumented services
#   3. Clears the agent's run history so the first assessment is a clean baseline
#
# Run demo-restore.sh after the "before" assessment to apply fixes and re-assess.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/otelcol-config.yml"
CONFIG_GOOD="$SCRIPT_DIR/otelcol-config.yml.good"
CONFIG_DEGRADED="$SCRIPT_DIR/otelcol-config.yml.degraded"

# ── Guard ───────────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_DEGRADED" ]]; then
  echo "ERROR: $CONFIG_DEGRADED not found. Make sure the file exists in the deploy directory."
  exit 1
fi

echo "=== DEMO DEGRADE ==="
echo "This will degrade the environment for Act 1. All services stay running except"
echo "recommendation, fraud-detection, accounting, and load-generator."
echo ""
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. Back up current good config ─────────────────────────────────────────
if [[ ! -f "$CONFIG_GOOD" ]]; then
  cp "$CONFIG" "$CONFIG_GOOD"
  echo "[1/4] Backed up current config → otelcol-config.yml.good"
else
  echo "[1/4] Good config backup already exists (otelcol-config.yml.good), skipping."
fi

# ── 2. Apply degraded OTel Collector config ─────────────────────────────────
cp "$CONFIG_DEGRADED" "$CONFIG"
echo "[2/4] Applied degraded OTel Collector config"
echo "      - resourcedetection.override: false  (host.name won't be stamped)"
echo "      - transform/promote_env_to_span removed  (deployment.environment not in span tags)"
echo "      - sf_environment not set  (environment scoping degraded)"

# ── 3. Restart collector with degraded config ───────────────────────────────
cd "$SCRIPT_DIR"
docker compose restart otel-collector > /dev/null
echo "[3/4] Restarted otel-collector with degraded config"

# ── 4. Stop services to create silent services ──────────────────────────────
SILENT_SERVICES="recommendation fraud-detection accounting load-generator"
for svc in $SILENT_SERVICES; do
  if docker ps --format '{{.Names}}' | grep -q "^${svc}$"; then
    docker stop "$svc" > /dev/null
    echo "      Stopped: $svc"
  else
    echo "      Already stopped: $svc"
  fi
done
echo "[4/4] Created silent services: $SILENT_SERVICES"

# ── 5. Clear agent run history for a clean baseline ─────────────────────────
ENV_NAME=$(docker inspect o11y-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | grep '^SPLUNK_ENVIRONMENT=' | cut -d= -f2-)
if [[ -n "$ENV_NAME" ]]; then
  docker exec o11y-agent sh -c "rm -f /root/.o11y-agent/${ENV_NAME}*.json" 2>/dev/null && \
    echo "[5/5] Cleared agent run history for environment: $ENV_NAME" || \
    echo "[5/5] Could not clear agent history (agent may not be running)"
else
  echo "[5/5] Could not determine SPLUNK_ENVIRONMENT — skipping history clear"
fi

echo ""
echo "======================================================"
echo " Environment is now degraded. Expected findings:"
echo "   - host.name missing from spans (coverage ~0%)"
echo "   - deployment.environment not in span tags"
echo "   - 4 silent services: recommendation, fraud-detection,"
echo "     accounting, load-generator"
echo "   - No sf_environment on metrics"
echo ""
echo " Next: trigger an assessment in the supervisor UI, then"
echo "       run ./demo-restore.sh to apply fixes."
echo "======================================================"
