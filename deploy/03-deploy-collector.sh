#!/usr/bin/env bash
# Deploy Splunk OTel Collector in gateway mode.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPLUNK_REALM="${SPLUNK_REALM:-us1}"
SPLUNK_ACCESS_TOKEN="${SPLUNK_ACCESS_TOKEN:?}"
SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"
CLUSTER_NAME="${CLUSTER_NAME:-o11y-demo}"

echo ""
echo "── Step 3: Deploy Splunk OTel Collector (gateway mode) ───────────────────"

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install splunk-otel-collector \
  splunk-otel-collector-chart/splunk-otel-collector \
  --namespace monitoring \
  --values "$SCRIPT_DIR/values/collector-values.yaml" \
  --set splunkObservability.accessToken="${SPLUNK_ACCESS_TOKEN}" \
  --set splunkObservability.realm="${SPLUNK_REALM}" \
  --set clusterName="${CLUSTER_NAME}" \
  --set splunkObservability.profilingEnabled=true \
  --wait --timeout=5m

echo "Collector deployed."
echo "Step 3 complete."
