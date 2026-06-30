#!/usr/bin/env bash
# Deploy RUM injector — Nginx proxy that injects Splunk RUM JavaScript into
# every HTML page served by the astronomy shop.
#
# After this step, users browsing the shop will send RUM telemetry (session data,
# Core Web Vitals, JavaScript errors) to Splunk Observability Cloud.
# Access the RUM-instrumented shop at: http://localhost:8081 (after port-forward)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SPLUNK_REALM="${SPLUNK_REALM:-us1}"
SPLUNK_ACCESS_TOKEN="${SPLUNK_ACCESS_TOKEN:?Need SPLUNK_ACCESS_TOKEN}"

echo ""
echo "── Step 6: Deploy RUM Injector ────────────────────────────────────────────"

# Patch the nginx config with actual realm + token
PATCHED_CONFIG=$(sed \
  -e "s/RUM_REALM/${SPLUNK_REALM}/g" \
  -e "s/RUM_AUTH_TOKEN/${SPLUNK_ACCESS_TOKEN}/g" \
  "$SCRIPT_DIR/rum-injector/configmap.yaml")

echo "$PATCHED_CONFIG" | kubectl apply -f -
kubectl apply -f "$SCRIPT_DIR/rum-injector/deployment.yaml"

echo "Waiting for rum-injector to be ready..."
kubectl rollout status deployment/rum-injector -n astronomy-shop --timeout=60s

echo ""
echo "RUM injector deployed."
echo ""
echo "Port-forward to access the RUM-instrumented shop:"
echo "  kubectl port-forward svc/rum-injector 8081:8081 -n astronomy-shop &"
echo "  open http://localhost:8081"
echo ""
echo "RUM data will appear in Splunk Observability Cloud:"
echo "  https://app.${SPLUNK_REALM}.signalfx.com/rum"
echo "  App name: astronomy-shop"
echo ""
echo "Step 6 complete."
