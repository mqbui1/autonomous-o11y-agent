#!/usr/bin/env bash
# demo-db-degrade.sh — Strip DB span attributes to simulate "no DB instrumentation".
#
# What this does:
#   1. Swaps in the DB-degraded OTel Collector config, which adds a
#      transform/strip_db_attrs processor that deletes db.system, db.name,
#      db.operation, db.statement from all spans
#   2. Restarts the collector with the degraded config
#   3. Clears agent run history for a clean baseline
#
# Effect: services making database calls (cart→Redis, etc.) continue to generate
# spans, but all DB-identifying attributes are stripped. The DB specialist sees
# outbound calls with no db.* context — zero DB instrumentation coverage.
#
# Run demo-db-restore.sh after the "before" assessment to re-enable DB attributes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/otelcol-config.yml"
CONFIG_GOOD="$SCRIPT_DIR/otelcol-config.yml.good"
CONFIG_DB_DEGRADED="$SCRIPT_DIR/otelcol-config.yml.db-degraded"

if [[ ! -f "$CONFIG_DB_DEGRADED" ]]; then
  echo "ERROR: $CONFIG_DB_DEGRADED not found."
  exit 1
fi

echo "=== DEMO: DB DEGRADE ==="
echo "Simulates: services making DB calls with no db.* span attributes."
echo "Effect:    DB specialist sees 0 DB instrumentation — all database"
echo "           dependencies are blind spots (no system, name, or operation)."
echo ""
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. Back up current good config ─────────────────────────────────────────
if [[ ! -f "$CONFIG_GOOD" ]]; then
  cp "$CONFIG" "$CONFIG_GOOD"
  echo "[1/3] Backed up current config → otelcol-config.yml.good"
else
  echo "[1/3] Good config backup already exists, skipping."
fi

# ── 2. Apply DB-degraded config ──────────────────────────────────────────────
cp "$CONFIG_DB_DEGRADED" "$CONFIG"
echo "[2/3] Applied DB-degraded OTel Collector config"
echo "      + transform/strip_db_attrs: deletes db.system, db.name, db.operation,"
echo "        db.statement, db.sql.table, db.redis.database_index"

# ── 3. Restart collector ─────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
docker compose restart otel-collector > /dev/null
echo "[3/3] Restarted otel-collector with DB-degraded config"

# ── 4. Clear agent run history ───────────────────────────────────────────────
ENV_NAME=$(docker inspect o11y-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | grep '^SPLUNK_ENVIRONMENT=' | cut -d= -f2-)
if [[ -n "$ENV_NAME" ]]; then
  docker exec o11y-agent sh -c "rm -f /home/agent/.o11y-agent/${ENV_NAME}*.json" 2>/dev/null && \
    echo "      Cleared agent run history for environment: $ENV_NAME"
fi

echo ""
echo "======================================================"
echo " Environment degraded (DB). Expected findings:"
echo "   - DB instrumentation score: 0/100"
echo "   - All services: db.* attributes missing"
echo "   - DB dependencies appear as unmonitored blind spots"
echo "   - Cannot identify slow queries, DB systems, or table names"
echo "   - cart→Redis calls visible as outbound spans but unattributed"
echo ""
echo " Wait ~2 minutes for spans to flow, then trigger an assessment."
echo " Next: run ./demo-db-restore.sh to re-enable DB attributes."
echo "======================================================"
