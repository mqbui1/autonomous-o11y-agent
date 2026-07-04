#!/usr/bin/env bash
# demo-restore.sh — Apply instrumentation fixes and restore services for demo Act 3.
#
# What this does:
#   1. Restores the good OTel Collector config (host.name stamping,
#      deployment.environment span tag promotion, sf_environment)
#   2. Restarts the collector with the fixed config
#   3. Restarts the 4 previously stopped services
#
# Run this after the "before" assessment to demonstrate the fix, then trigger
# another assessment to show the improved score in the compare view.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/otelcol-config.yml"
CONFIG_GOOD="$SCRIPT_DIR/otelcol-config.yml.good"

# ── Guard ───────────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_GOOD" ]]; then
  echo "ERROR: No backup found at otelcol-config.yml.good"
  echo "Run ./demo-degrade.sh first to create the backup."
  exit 1
fi

echo "=== DEMO RESTORE ==="
echo "This will apply the instrumentation fixes and restart stopped services."
echo ""

# ── 1. Restore good OTel Collector config ───────────────────────────────────
cp "$CONFIG_GOOD" "$CONFIG"
echo "[1/3] Restored good OTel Collector config"
echo "      + resourcedetection.override: true  (host.name stamped on all spans/metrics)"
echo "      + transform/promote_env_to_span  (deployment.environment in span tags)"
echo "      + sf_environment set on all metrics"

# ── 2. Restart collector with fixed config ───────────────────────────────────
cd "$SCRIPT_DIR"
docker compose restart otel-collector > /dev/null
echo "[2/3] Restarted otel-collector with fixed config"

# ── 3. Restart stopped services ─────────────────────────────────────────────
RESTORED_SERVICES="recommendation fraud-detection accounting load-generator"
for svc in $RESTORED_SERVICES; do
  if docker ps -a --format '{{.Names}}' | grep -q "^${svc}$"; then
    docker start "$svc" > /dev/null
    echo "      Started: $svc"
  else
    echo "      Not found (may not be in this compose stack): $svc"
  fi
done
echo "[3/3] Restarted services: $RESTORED_SERVICES"

echo ""
echo "======================================================"
echo " Environment restored. Fixes applied:"
echo "   + host.name now stamped on all spans/metrics"
echo "   + deployment.environment now appears as span tag"
echo "   + sf_environment set for correct environment scoping"
echo "   + 4 services restarted: recommendation, fraud-detection,"
echo "     accounting, load-generator"
echo ""
echo " Next: trigger a new assessment in the supervisor UI, then"
echo "       use the Compare view to show the score improvement."
echo "======================================================"
