#!/usr/bin/env bash
# demo-rum-restore.sh — Re-enable RUM by injecting the token back into nginx.
#
# What this does:
#   1. Reads SPLUNK_RUM_TOKEN and SPLUNK_REALM from .env
#   2. Patches the rum-proxy nginx config with the real token and correct beacon endpoint
#   3. Reloads nginx so sessions start flowing immediately
#
# Run this after the "before" assessment. Trigger another assessment afterward
# and use the Compare view to show the RUM score jump.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ── Load credentials ─────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

RUM_TOKEN=$(grep '^SPLUNK_RUM_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
REALM=$(grep '^SPLUNK_REALM=' "$ENV_FILE" | cut -d= -f2-)
ENVIRONMENT=$(grep '^SPLUNK_ENVIRONMENT=' "$ENV_FILE" | cut -d= -f2-)

if [[ -z "$RUM_TOKEN" ]]; then
  echo "ERROR: SPLUNK_RUM_TOKEN is empty in .env"
  echo "Create a RUM token at: https://app.${REALM:-us1}.signalfx.com/o11y/integrations/rum"
  echo "Then set SPLUNK_RUM_TOKEN=<token> in deploy/.env"
  exit 1
fi

echo "=== DEMO: RUM RESTORE ==="
echo "Enabling RUM instrumentation for environment: ${ENVIRONMENT:-astroshop-local}"
echo "Token: ${RUM_TOKEN:0:8}..."
echo ""

# ── 1. Patch rum-proxy nginx with real token + correct beacon endpoint ────────
docker exec rum-proxy sh -c "
  sed -i \
    -e 's|rumAccessToken:\"[^\"]*\"|rumAccessToken:\"${RUM_TOKEN}\"|g' \
    -e 's|beaconEndpoint:\"https://rum\\.${REALM}\\.signalfx\\.com/v1/rum\"|beaconEndpoint:\"https://rum-ingest.${REALM}.signalfx.com/v1/rum\"|g' \
    /etc/nginx/conf.d/default.conf &&
  nginx -s reload
" 2>/dev/null
echo "[1/1] Patched rum-proxy nginx with RUM token and reloaded"

echo ""
echo "======================================================"
echo " RUM enabled. What happens next:"
echo "   - Browser sessions now send beacons to rum-ingest.${REALM:-us1}.signalfx.com"
echo "   - Sessions appear in Splunk RUM within 5-10 minutes"
echo "   - App name: astroshop  |  Environment: ${ENVIRONMENT:-astroshop-local}"
echo ""
echo " Next: trigger a new assessment in the supervisor UI, then"
echo "       use the Compare view to show the RUM score improvement."
echo "   RUM dashboard: https://app.${REALM:-us1}.signalfx.com/rum"
echo "======================================================"
