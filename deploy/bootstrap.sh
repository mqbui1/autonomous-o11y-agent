#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# bootstrap.sh — One-shot EC2 deploy for o11y-agent + astronomy shop
#
# Run from your LOCAL machine (Mac):
#   export SPLUNK_ACCESS_TOKEN=<ingest-token>
#   export SPLUNK_REALM=us1
#   export EC2_HOST=<ip>
#   bash deploy/bootstrap.sh
#
# AWS creds are auto-exported from your current AWS SSO session.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${SPLUNK_ACCESS_TOKEN:?Set SPLUNK_ACCESS_TOKEN}"
: "${SPLUNK_REALM:?Set SPLUNK_REALM}"
: "${EC2_HOST:?Set EC2_HOST}"

EC2_PASS="${EC2_PASS:-Sp1unkH00di3}"
EC2_PORT="${EC2_PORT:-2222}"
SPLUNK_ENVIRONMENT="${SPLUNK_ENVIRONMENT:-astronomy-shop-demo}"

# ── Export AWS creds from current SSO session ─────────────────────────────
echo "Exporting AWS credentials from SSO session..."
eval "$(aws configure export-credentials --format env)" || {
  echo "ERROR: aws configure export-credentials failed. Run: aws sso login"; exit 1
}
: "${AWS_ACCESS_KEY_ID:?AWS creds not found}"
echo "AWS: $(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"

# ── Bake credentials into the remote run script ───────────────────────────
# NOTE: Use a temp file to avoid passing secrets on the command line.
TMPSCRIPT=$(mktemp /tmp/run-deploy-XXXXXX.sh)
trap "rm -f $TMPSCRIPT" EXIT

cat > "$TMPSCRIPT" << REMOTE_EOF
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

# ── Step 0: Install tools if missing ─────────────────────────────────────
echo ""
echo "── Step 0: Install tools ────────────────────────────────────────────"

# Wait for apt lock (unattended-upgrades holds it on fresh boot)
echo "Waiting for apt lock to clear..."
sudo systemctl stop unattended-upgrades 2>/dev/null || true
for i in \$(seq 1 30); do
  if ! sudo fuser /var/lib/dpkg/lock-frontend &>/dev/null; then break; fi
  echo "  apt locked, waiting... (\$i/30)"
  sleep 5
done
sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/lib/dpkg/lock 2>/dev/null || true
sudo dpkg --configure -a 2>/dev/null || true

# Docker
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  # Remove any broken apt sources first
  sudo rm -f /etc/apt/sources.list.d/splunk* 2>/dev/null || true
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker splunk || true
  sudo systemctl enable --now docker
else
  echo "Docker: \$(docker --version)"
fi

# Ensure docker is accessible without sudo
if ! docker ps &>/dev/null; then
  sudo chmod 666 /var/run/docker.sock 2>/dev/null || true
fi

# k3d
if ! command -v k3d &>/dev/null; then
  echo "Installing k3d..."
  curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
else
  echo "k3d: \$(k3d version | head -1)"
fi

# kubectl
if ! command -v kubectl &>/dev/null; then
  echo "Installing kubectl..."
  sudo snap install kubectl --classic 2>/dev/null || \
    (KUBE_VER=\$(curl -sSL https://dl.k8s.io/release/stable.txt) && \
     curl -sSLo /tmp/kubectl "https://dl.k8s.io/release/\${KUBE_VER}/bin/linux/amd64/kubectl" && \
     sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl)
else
  echo "kubectl: \$(kubectl version --client --short 2>/dev/null | head -1 || kubectl version --client | head -1)"
fi

# helm
if ! command -v helm &>/dev/null; then
  echo "Installing Helm..."
  curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
else
  echo "Helm: \$(helm version --short)"
fi

# tmux
if ! command -v tmux &>/dev/null; then
  sudo apt-get install -y tmux 2>/dev/null || true
fi

echo "Tools ready."

# ── Step 1: Clone repos if missing ────────────────────────────────────────
echo ""
echo "── Step 1: Clone repos ──────────────────────────────────────────────"

REPOS=(
  "autonomous-o11y-agent"
  "auto-detector-provisioner"
  "o11y-usage-governance"
  "o11y-instrumentation-analyzer"
  "splunk-o11y-health-check"
)
for repo in "\${REPOS[@]}"; do
  if [ -d "\$PARENT_DIR/\$repo" ]; then
    echo "  \$repo: already cloned"
    cd "\$PARENT_DIR/\$repo" && git pull --rebase --autostash 2>/dev/null || true
  else
    echo "  Cloning \$repo..."
    git clone --depth=1 "https://github.com/mqbui1/\$repo" "\$PARENT_DIR/\$repo"
  fi
done
echo "Repos ready. Agent: \$(cd \$REPO_DIR && git log -1 --oneline)"

# ── Step 2: k3d cluster ───────────────────────────────────────────────────
echo ""
echo "── Step 2: k3d cluster ──────────────────────────────────────────────"

if k3d cluster list 2>/dev/null | grep -q "^\${CLUSTER_NAME}"; then
  echo "Cluster '\$CLUSTER_NAME' already exists."
else
  echo "Creating k3d cluster '\$CLUSTER_NAME'..."
  k3d cluster create "\$CLUSTER_NAME" \
    --servers 1 --agents 2 \
    --port "8080:80@loadbalancer" \
    --port "4317:4317@loadbalancer" \
    --port "4318:4318@loadbalancer" \
    --wait
fi
kubectl config use-context "k3d-\${CLUSTER_NAME}"
echo "Cluster ready: \$(kubectl cluster-info 2>/dev/null | head -1)"

# ── Step 3: Helm repos ────────────────────────────────────────────────────
echo ""
echo "── Step 3: Helm repos ───────────────────────────────────────────────"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>/dev/null || true
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart 2>/dev/null || true
helm repo update 2>&1 | tail -3

# ── Step 4: Docker image — GHCR pull first, local build fallback ──────────
echo ""
echo "── Step 4: Docker image ─────────────────────────────────────────────"

if docker image inspect "\$AGENT_IMAGE" &>/dev/null; then
  echo "Image already present: \$AGENT_IMAGE"
elif docker pull "\$GHCR_IMAGE" 2>/dev/null; then
  docker tag "\$GHCR_IMAGE" "\$AGENT_IMAGE"
  echo "Pulled from GHCR: \$GHCR_IMAGE"
else
  echo "Building locally (GHCR not available)..."
  DOCKER_BUILDKIT=0 docker build \
    -t "\$AGENT_IMAGE" \
    -f "\$REPO_DIR/Dockerfile" \
    "\$PARENT_DIR"
fi

echo "Importing into k3d cluster '\$CLUSTER_NAME'..."
k3d image import "\$AGENT_IMAGE" -c "\$CLUSTER_NAME"
echo "Image ready."

# ── Step 5: Splunk OTel Collector ─────────────────────────────────────────
echo ""
echo "── Step 5: Splunk OTel Collector ────────────────────────────────────"
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

# ── Step 6: Astronomy Shop ────────────────────────────────────────────────
echo ""
echo "── Step 6: Astronomy Shop ───────────────────────────────────────────"
kubectl create namespace astronomy-shop --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install astronomy-shop \
  open-telemetry/opentelemetry-demo \
  --namespace astronomy-shop \
  --values "\$REPO_DIR/deploy/values/astronomy-shop-values.yaml" \
  --wait --timeout=10m
echo "Astronomy Shop deployed."

# ── Step 7: o11y-agent ────────────────────────────────────────────────────
echo ""
echo "── Step 7: o11y-agent ───────────────────────────────────────────────"
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

# Patch collector to fan out to agent
echo "Patching collector → o11y-agent fanout..."
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
  echo "WARNING: gateway-patch CM not found — skipping."
fi

# ── Step 8: RUM injector ──────────────────────────────────────────────────
echo ""
echo "── Step 8: RUM injector ─────────────────────────────────────────────"
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
kubectl get pods -n monitoring
kubectl get pods -n astronomy-shop | head -15
echo ""
echo "  Shop (plain): kubectl port-forward svc/astronomy-shop-frontendproxy 8080:8080 -n astronomy-shop"
echo "  Shop (RUM):   kubectl port-forward svc/rum-injector 8081:8081 -n astronomy-shop"
echo "  Agent logs:   kubectl logs -f deployment/o11y-agent -n monitoring"
echo "================================================================"
REMOTE_EOF

# ── Upload script and start in single SSH connection ─────────────────────
echo ""
echo "Uploading run script to ${EC2_HOST}:${EC2_PORT}..."
sshpass -p "${EC2_PASS}" scp \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -P "${EC2_PORT}" \
  "$TMPSCRIPT" \
  "splunk@${EC2_HOST}:/tmp/run-deploy.sh"

echo "Starting deploy in tmux session 'deploy'..."
sshpass -p "${EC2_PASS}" ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -o NumberOfPasswordPrompts=1 \
  -p "${EC2_PORT}" \
  "splunk@${EC2_HOST}" \
  "tmux kill-session -t deploy 2>/dev/null || true; \
   tmux new-session -d -s deploy 'bash /tmp/run-deploy.sh'; \
   echo 'Deploy started. Monitor with:'; \
   echo '  tail -f /tmp/deploy.log'"

echo ""
echo "Deploy running on ${EC2_HOST}. Tailing log (Ctrl+C to detach)..."
echo ""
sshpass -p "${EC2_PASS}" ssh \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -o NumberOfPasswordPrompts=1 \
  -p "${EC2_PORT}" \
  "splunk@${EC2_HOST}" \
  'tail -f /tmp/deploy.log'
