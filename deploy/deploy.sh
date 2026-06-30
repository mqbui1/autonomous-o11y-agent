#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Full end-to-end deployment: Astronomy Shop + Splunk OTel Collector + O11y Agent
#
# Usage:
#   export SPLUNK_ACCESS_TOKEN=<your-token>
#   export SPLUNK_REALM=us1
#   export AWS_ACCESS_KEY_ID=<key>
#   export AWS_SECRET_ACCESS_KEY=<secret>
#   export AWS_DEFAULT_REGION=us-west-2        # optional, default: us-west-2
#   export SPLUNK_ENVIRONMENT=astronomy-shop-demo  # optional
#   export CLUSTER_NAME=o11y-demo              # optional
#   chmod +x deploy/deploy.sh && ./deploy/deploy.sh
#
# What it does:
#   1. Creates a k3d cluster
#   2. Builds the o11y-agent Docker image and loads it into the cluster
#   3. Deploys the Splunk OTel Collector (gateway mode)
#   4. Deploys the OpenTelemetry Astronomy Shop demo
#   5. Deploys the o11y-agent (streaming mode, alongside the collector)
#   6. Patches the collector to fan out telemetry to the agent
#   7. Runs the first batch assessment
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"

# ── Required env vars ─────────────────────────────────────────────────────────
: "${SPLUNK_ACCESS_TOKEN:?SPLUNK_ACCESS_TOKEN is required}"
: "${SPLUNK_REALM:?SPLUNK_REALM is required}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"

SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"
CLUSTER_NAME="${CLUSTER_NAME:-o11y-demo}"
AGENT_IMAGE="o11y-agent:latest"

echo "================================================================"
echo "  O11y Agent — Full Stack Deployment"
echo "  realm=$SPLUNK_REALM  env=$SPLUNK_ENVIRONMENT  cluster=$CLUSTER_NAME"
echo "================================================================"

source "$SCRIPT_DIR/01-setup.sh"
source "$SCRIPT_DIR/02-build.sh"
source "$SCRIPT_DIR/03-deploy-collector.sh"
source "$SCRIPT_DIR/04-deploy-astronomy-shop.sh"
source "$SCRIPT_DIR/05-deploy-agent.sh"
source "$SCRIPT_DIR/06-enable-rum.sh"

echo ""
echo "================================================================"
echo "  Deployment complete!"
echo ""
echo "  Astronomy Shop (plain):  kubectl port-forward svc/astronomy-shop-frontendproxy 8080:8080 -n astronomy-shop"
echo "                           then open http://localhost:8080"
echo ""
echo "  Astronomy Shop (RUM):    kubectl port-forward svc/rum-injector 8081:8081 -n astronomy-shop"
echo "                           then open http://localhost:8081 (RUM data flows to Splunk)"
echo ""
echo "  Agent status:    kubectl logs -f deployment/o11y-agent -n monitoring"
echo "  Agent health:    kubectl port-forward svc/o11y-agent 4318:4318 -n monitoring"
echo "                   curl http://localhost:4318/healthz"
echo "                   curl http://localhost:4318/status"
echo ""
echo "  Splunk O11y:     https://app.${SPLUNK_REALM}.signalfx.com"
echo "                   APM > Services: environment=astronomy-shop-demo"
echo "                   RUM > Applications: app=astronomy-shop"
echo "================================================================"
