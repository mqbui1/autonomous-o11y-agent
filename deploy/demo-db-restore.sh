#!/usr/bin/env bash
# demo-db-restore.sh — Restore DB span attributes by removing the strip processor.
#
# What this does:
#   1. Restores the good OTel Collector config (removes transform/strip_db_attrs)
#   2. Restarts the collector — db.system, db.name, db.operation, db.statement
#      immediately start flowing on all spans again
#
# Run this after the "before" assessment. Trigger another assessment and use
# the Compare view to show DB instrumentation coverage going from 0 → covered.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/otelcol-config.yml"
CONFIG_GOOD="$SCRIPT_DIR/otelcol-config.yml.good"

if [[ ! -f "$CONFIG_GOOD" ]]; then
  echo "ERROR: No backup at otelcol-config.yml.good"
  echo "Run ./demo-db-degrade.sh first."
  exit 1
fi

echo "=== DEMO: DB RESTORE ==="
echo "Restoring DB span attributes (db.system, db.name, db.operation, db.statement)."
echo ""

# ── 1. Restore good config ──────────────────────────────────────────────────
cp "$CONFIG_GOOD" "$CONFIG"
echo "[1/2] Restored good OTel Collector config (transform/strip_db_attrs removed)"

# ── 2. Restart collector ─────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
docker compose restart otel-collector > /dev/null
echo "[2/2] Restarted otel-collector with DB attributes restored"

echo ""
echo "======================================================"
echo " DB instrumentation restored. What happens next:"
echo "   + db.system, db.name, db.operation appear on all DB spans"
echo "   + DB specialist can now map: cart → Redis, checkout → ..."
echo "   + Slow query identification enabled (db.statement visible)"
echo "   + APM Database Overview page populated"
echo ""
echo " Wait ~2 minutes for spans to flow, then trigger a new assessment."
echo " Use the Compare view to show DB coverage jumping from 0 → instrumented."
echo "======================================================"
