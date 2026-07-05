#!/usr/bin/env bash
# demo-rum-degrade.sh — Remove RUM token to simulate "RUM not configured" state.
#
# What this does:
#   1. Patches the rum-proxy nginx config to clear the rumAccessToken
#   2. Reloads nginx so the change takes effect immediately
#   3. Clears agent run history for a clean baseline
#
# Run demo-rum-restore.sh after the "before" assessment to re-enable RUM.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== DEMO: RUM DEGRADE ==="
echo "Simulates: frontend not instrumented with Splunk RUM."
echo "Effect:    rumAccessToken cleared in nginx → browser beacons rejected → 0 sessions."
echo ""
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. Clear RUM token in rum-proxy nginx config ─────────────────────────────
docker exec rum-proxy sh -c "
  sed -i 's|rumAccessToken:\"[^\"]*\"|rumAccessToken:\"\"|g' /etc/nginx/conf.d/default.conf &&
  nginx -s reload
" 2>/dev/null
echo "[1/2] Cleared rumAccessToken in rum-proxy nginx config and reloaded"

# ── 2. Clear agent run history ───────────────────────────────────────────────
ENV_NAME=$(docker inspect o11y-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | grep '^SPLUNK_ENVIRONMENT=' | cut -d= -f2-)
if [[ -n "$ENV_NAME" ]]; then
  docker exec o11y-agent sh -c "rm -f /home/agent/.o11y-agent/${ENV_NAME}*.json" 2>/dev/null && \
    echo "[2/2] Cleared agent run history for environment: $ENV_NAME" || \
    echo "[2/2] Could not clear agent history (agent may not be running)"
else
  echo "[2/2] Could not determine SPLUNK_ENVIRONMENT — skipping history clear"
fi

echo ""
echo "======================================================"
echo " Environment degraded (RUM). Expected findings:"
echo "   - RUM score: 0/100"
echo "   - 0 sessions, 0 JS errors, Core Web Vitals unmeasured"
echo "   - astroshop-frontend reported as unconfigured"
echo ""
echo " Next: trigger an assessment in the supervisor UI, then"
echo "       run ./demo-rum-restore.sh to re-enable RUM."
echo "======================================================"
