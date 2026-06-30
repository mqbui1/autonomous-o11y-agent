#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# bootstrap.sh — One-shot EC2 deploy for o11y-agent + astronomy shop
#
# Run from your LOCAL machine (Mac):
#   1. Set env vars (see below)
#   2. bash deploy/bootstrap.sh
#
# It will:
#   a. Upload a self-contained run script to the EC2 instance
#   b. Start it in a single tmux session (safe from SSH disconnects)
#   c. Tail the log so you can monitor progress
#
# Required env vars:
#   SPLUNK_ACCESS_TOKEN  — ingest/access token
#   SPLUNK_REALM         — e.g. us1
#   EC2_HOST             — EC2 IP address
#   EC2_PASS             — SSH password (default: Sp1unkH00di3)
#   EC2_PORT             — SSH port (default: 2222)
#
# AWS creds are auto-exported from your current AWS SSO session.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${SPLUNK_ACCESS_TOKEN:?Set SPLUNK_ACCESS_TOKEN}"
: "${SPLUNK_REALM:?Set SPLUNK_REALM}"
: "${EC2_HOST:?Set EC2_HOST (EC2 IP)}"

EC2_PASS="${EC2_PASS:-Sp1unkH00di3}"
EC2_PORT="${EC2_PORT:-2222}"
SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"

# ── Export AWS creds from current SSO session ─────────────────────────────
echo "Exporting AWS credentials from current SSO session..."
eval "$(aws configure export-credentials --format env)" || {
  echo "ERROR: Failed to export AWS credentials."
  echo "       Make sure you're logged in: aws sso login"
  exit 1
}
: "${AWS_ACCESS_KEY_ID:?AWS creds not found}"
echo "AWS identity: $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo unknown)"

# ── Build the remote run script (heredoc with vars substituted) ───────────
REMOTE_SCRIPT=$(cat << REMOTE_EOF
#!/usr/bin/env bash
set -euo pipefail
exec > >(tee -a /tmp/deploy.log) 2>&1

export SPLUNK_ACCESS_TOKEN='${SPLUNK_ACCESS_TOKEN}'
export SPLUNK_REALM='${SPLUNK_REALM}'
export SPLUNK_ENVIRONMENT='${SPLUNK_ENVIRONMENT}'
export AWS_ACCESS_KEY_ID='${AWS_ACCESS_KEY_ID}'
export AWS_SECRET_ACCESS_KEY='${AWS_SECRET_ACCESS_KEY}'
export AWS_SESSION_TOKEN='${AWS_SESSION_TOKEN:-}'
export AWS_DEFAULT_REGION='${AWS_DEFAULT_REGION:-us-west-2}'

source /etc/environment 2>/dev/null || true
export CLUSTER_NAME="\${CLUSTER_NAME:-o11y-demo}"
export AGENT_IMAGE="o11y-agent:latest"
REPO_DIR="\$HOME/autonomous-o11y-agent"
PARENT_DIR="\$HOME"
GHCR_IMAGE="ghcr.io/mqbui1/o11y-agent:latest"

echo ""
echo "================================================================"
echo "  O11y Agent — Full Stack Deploy  \$(date)"
echo "  cluster=\$CLUSTER_NAME  env=\$SPLUNK_ENVIRONMENT"
echo "================================================================"

# ── Ensure kubectl context ────────────────────────────────────────────────
kubectl config use-context "k3d-\${CLUSTER_NAME}" 2>/dev/null || true

# ── Pull latest code ──────────────────────────────────────────────────────
echo ""
echo "── Pull latest code ─────────────────────────────────────────────────"
cd "\$REPO_DIR" && git pull --rebase --autostash
echo "Git: \$(git log -1 --oneline)"

# ── Helm repos ────────────────────────────────────────────────────────────
echo ""
echo "── Helm repos ───────────────────────────────────────────────────────"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>/dev/null || true
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart 2>/dev/null || true
helm repo update 2>&1 | tail -3

# ── Docker image — try GHCR pull first, fall back to local build ──────────
echo ""
echo "── Docker image ─────────────────────────────────────────────────────"
if docker image inspect "\$AGENT_IMAGE" &>/dev/null; then
  echo "Image already present locally: \$AGENT_IMAGE"
elif docker pull "\$GHCR_IMAGE" 2>/dev/null; then
  docker tag "\$GHCR_IMAGE" "\$AGENT_IMAGE"
  echo "Pulled from GHCR."
else
  echo "Building locally (GHCR not available)..."
  # Clone any missing sibling repos
  for repo in auto-detector-provisioner o11y-usage-governance o11y-instrumentation-analyzer splunk-o11y-health-check; do
    [ -d "\$PARENT_DIR/\$repo" ] || git clone --depth=1 "https://github.com/mqbui1/\$repo" "\$PARENT_DIR/\$repo"
  done
  DOCKER_BUILDKIT=0 docker build -t "\$AGENT_IMAGE" -f "\$REPO_DIR/Dockerfile" "\$PARENT_DIR"
fi
echo "Importing into k3d cluster '\$CLUSTER_NAME'..."
k3d image import "\$AGENT_IMAGE" -c "\$CLUSTER_NAME"
echo "Image ready."

# ── Splunk OTel Collector ─────────────────────────────────────────────────
echo ""
echo "── Splunk OTel Collector ─────────────────────────────────────────────"
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install splunk-otel-collector \
  splunk-otel-collector-chart/splunk-otel-collector \
  --namespace monitoring \
  --values "\$REPO_DIR/deploy/values/collector-values.yaml" \
  --set splunkObservability.accessToken="\$SPLUNK_ACCESS_TOKEN" \
  --set splunkObservability.realm="\$SPLUNK_REALM" \
  --set clusterName="\$CLUSTER_NAME" \
  --set splunkObservability.profilingEnabled=true \
  --wait --timeout=5m
echo "Collector deployed."

# ── Astronomy Shop ────────────────────────────────────────────────────────
echo ""
echo "── Astronomy Shop ───────────────────────────────────────────────────"
kubectl create namespace astronomy-shop --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install astronomy-shop \
  open-telemetry/opentelemetry-demo \
  --namespace astronomy-shop \
  --values "\$REPO_DIR/deploy/values/astronomy-shop-values.yaml" \
  --wait --timeout=10m
echo "Astronomy Shop deployed."

# ── o11y-agent ────────────────────────────────────────────────────────────
echo ""
echo "── o11y-agent ───────────────────────────────────────────────────────"
SESSION_TOKEN_ARG=""
[ -n "\${AWS_SESSION_TOKEN:-}" ] && SESSION_TOKEN_ARG="--set aws.sessionToken=\${AWS_SESSION_TOKEN}"
helm upgrade --install o11y-agent \
  "\$REPO_DIR/charts/o11y-agent" \
  --namespace monitoring \
  --values "\$REPO_DIR/deploy/values/agent-values.yaml" \
  --set splunk.realm="\$SPLUNK_REALM" \
  --set splunk.accessToken="\$SPLUNK_ACCESS_TOKEN" \
  --set splunk.environment="\$SPLUNK_ENVIRONMENT" \
  --set aws.accessKeyId="\$AWS_ACCESS_KEY_ID" \
  --set aws.secretAccessKey="\$AWS_SECRET_ACCESS_KEY" \
  \${SESSION_TOKEN_ARG} \
  --set aws.region="\$AWS_DEFAULT_REGION" \
  --set image.repository="o11y-agent" \
  --set image.tag="latest" \
  --set image.pullPolicy=Never \
  --wait --timeout=3m
echo "o11y-agent deployed."

# ── Patch collector to fan out to agent ──────────────────────────────────
echo ""
echo "── Patching collector to fan out to o11y-agent ──────────────────────"
for i in \$(seq 1 12); do
  kubectl get cm o11y-agent-gateway-patch -n monitoring &>/dev/null && break
  echo "  Waiting for gateway-patch CM... (\$i/12)"
  sleep 5
done
PATCH_VALUES=\$(kubectl get cm o11y-agent-gateway-patch -n monitoring \
  -o jsonpath='{.data.values\.yaml}' 2>/dev/null || echo "")
if [ -n "\$PATCH_VALUES" ]; then
  echo "\$PATCH_VALUES" | helm upgrade splunk-otel-collector \
    splunk-otel-collector-chart/splunk-otel-collector \
    --namespace monitoring --reuse-values -f - --wait --timeout=3m
  echo "Collector patched."
else
  echo "WARNING: gateway-patch CM not found — skipping collector patch."
fi

# ── RUM injector ──────────────────────────────────────────────────────────
echo ""
echo "── RUM injector ─────────────────────────────────────────────────────"
sed \
  -e "s/RUM_REALM/\${SPLUNK_REALM}/g" \
  -e "s/RUM_AUTH_TOKEN/\${SPLUNK_ACCESS_TOKEN}/g" \
  "\$REPO_DIR/deploy/rum-injector/configmap.yaml" | kubectl apply -f -
kubectl apply -f "\$REPO_DIR/deploy/rum-injector/deployment.yaml"
kubectl rollout status deployment/rum-injector -n astronomy-shop --timeout=60s
echo "RUM injector deployed."

# ── Done ──────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  DEPLOYMENT COMPLETE  \$(date)"
echo ""
echo "  Pods:"
kubectl get pods -n monitoring
kubectl get pods -n astronomy-shop | head -10
echo ""
echo "  Shop (plain): kubectl port-forward svc/astronomy-shop-frontendproxy 8080:8080 -n astronomy-shop"
echo "  Shop (RUM):   kubectl port-forward svc/rum-injector 8081:8081 -n astronomy-shop"
echo "  Agent logs:   kubectl logs -f deployment/o11y-agent -n monitoring"
echo "================================================================"
REMOTE_EOF
)

# ── Upload env file and run script via single SSH connection ──────────────
echo ""
echo "Uploading and starting deploy on ${EC2_HOST}:${EC2_PORT}..."
echo "(Credentials passed via file, not command-line — fail2ban safe)"

sshpass -p "${EC2_PASS}" ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -o NumberOfPasswordPrompts=1 \
  -p "${EC2_PORT}" \
  "splunk@${EC2_HOST}" \
  "cat > /tmp/run-deploy.sh && chmod +x /tmp/run-deploy.sh && \
   tmux kill-session -t deploy 2>/dev/null || true && \
   tmux new-session -d -s deploy 'bash /tmp/run-deploy.sh' && \
   echo 'Deploy started in tmux session: deploy'" \
  <<< "${REMOTE_SCRIPT}"

echo ""
echo "Tailing /tmp/deploy.log (Ctrl+C to detach — deploy continues in tmux)..."
echo ""
sshpass -p "${EC2_PASS}" ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -o NumberOfPasswordPrompts=1 \
  -p "${EC2_PORT}" \
  "splunk@${EC2_HOST}" \
  'tail -f /tmp/deploy.log'
