#!/usr/bin/env bash
# Deploy the o11y-agent in streaming mode alongside the gateway collector,
# then patch the collector to fan out telemetry to the agent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SPLUNK_REALM="${SPLUNK_REALM:-us1}"
SPLUNK_ACCESS_TOKEN="${SPLUNK_ACCESS_TOKEN:?}"
SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:?}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:?}"
AWS_SESSION_TOKEN="${AWS_SESSION_TOKEN:-}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"
AGENT_IMAGE="${AGENT_IMAGE:-o11y-agent:latest}"

echo ""
echo "── Step 5: Deploy O11y Agent (streaming mode) ────────────────────────────"

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install o11y-agent \
  "$REPO_DIR/charts/o11y-agent" \
  --namespace monitoring \
  --values "$SCRIPT_DIR/values/agent-values.yaml" \
  --set splunk.realm="${SPLUNK_REALM}" \
  --set splunk.accessToken="${SPLUNK_ACCESS_TOKEN}" \
  --set splunk.environment="${SPLUNK_ENVIRONMENT}" \
  --set aws.accessKeyId="${AWS_ACCESS_KEY_ID}" \
  --set aws.secretAccessKey="${AWS_SECRET_ACCESS_KEY}" \
  ${AWS_SESSION_TOKEN:+--set aws.sessionToken="${AWS_SESSION_TOKEN}"} \
  --set aws.region="${AWS_DEFAULT_REGION}" \
  --set image.repository="${AGENT_IMAGE%%:*}" \
  --set image.tag="${AGENT_IMAGE##*:}" \
  --set image.pullPolicy=Never \
  --wait --timeout=3m

echo "O11y Agent deployed."

# ── Patch collector to fan out to agent ───────────────────────────────────────
echo ""
echo "Patching Splunk OTel Collector to fan out telemetry to o11y-agent..."

# Wait for the gateway-patch ConfigMap to be created by the post-install hook
echo "Waiting for gateway-patch ConfigMap..."
for i in {1..12}; do
  if kubectl get cm o11y-agent-gateway-patch -n monitoring &>/dev/null; then
    break
  fi
  echo "  Waiting... ($i/12)"
  sleep 5
done

# Apply the patch to the collector
PATCH_VALUES=$(kubectl get cm o11y-agent-gateway-patch \
  -n monitoring \
  -o jsonpath='{.data.values\.yaml}' 2>/dev/null)

if [ -z "$PATCH_VALUES" ]; then
  echo "WARNING: gateway-patch ConfigMap not found or empty — skipping collector patch."
  echo "  Run manually after deploy:"
  echo "  kubectl get cm o11y-agent-gateway-patch -n monitoring -o jsonpath='{.data.values\\.yaml}' | helm upgrade splunk-otel-collector splunk-otel-collector-chart/splunk-otel-collector -n monitoring -f -"
else
  echo "$PATCH_VALUES" | helm upgrade splunk-otel-collector \
    splunk-otel-collector-chart/splunk-otel-collector \
    --namespace monitoring \
    --reuse-values \
    -f - \
    --wait --timeout=3m
  echo "Collector patched — telemetry now fans out to o11y-agent."
fi

# ── Verify deployment ──────────────────────────────────────────────────────────
echo ""
echo "Deployment summary:"
kubectl get pods -n monitoring
echo ""
echo "O11y Agent logs (last 20 lines):"
kubectl logs deployment/o11y-agent -n monitoring --tail=20 2>/dev/null || \
  echo "  (pod may still be starting)"

echo ""
echo "Step 5 complete."
