#!/usr/bin/env bash
# Deploy the OpenTelemetry Astronomy Shop demo app.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"

echo ""
echo "── Step 4: Deploy OpenTelemetry Astronomy Shop ───────────────────────────"

kubectl create namespace astronomy-shop --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install astronomy-shop \
  open-telemetry/opentelemetry-demo \
  --namespace astronomy-shop \
  --values "$SCRIPT_DIR/values/astronomy-shop-values.yaml" \
  --set "opentelemetry-collector.config.exporters.otlp/splunk.endpoint=splunk-otel-collector-gateway.monitoring.svc.cluster.local:4317" \
  --wait --timeout=10m

echo "Astronomy Shop deployed."
kubectl get pods -n astronomy-shop

echo ""
echo "  Waiting for all pods to be running..."
kubectl wait --for=condition=Ready pod --all -n astronomy-shop --timeout=300s || \
  echo "  Note: some pods may still be starting — check with: kubectl get pods -n astronomy-shop"

echo "Step 4 complete."
