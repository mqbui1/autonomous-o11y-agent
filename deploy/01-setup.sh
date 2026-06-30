#!/usr/bin/env bash
# Install k3d, kubectl, helm on a fresh Ubuntu EC2 instance.
# Safe to re-run — skips already-installed tools.
set -euo pipefail

echo ""
echo "── Step 1: Install dependencies ──────────────────────────────────────────"

# Docker
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "$(whoami)" || true
  systemctl enable --now docker
  # If running as non-root, reload group (needed for k3d)
  newgrp docker <<GRPEOF || true
GRPEOF
else
  echo "Docker already installed: $(docker --version)"
fi

# k3d
if ! command -v k3d &>/dev/null; then
  echo "Installing k3d..."
  curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
else
  echo "k3d already installed: $(k3d version | head -1)"
fi

# kubectl
if ! command -v kubectl &>/dev/null; then
  echo "Installing kubectl..."
  KUBE_VER=$(curl -sSL https://dl.k8s.io/release/stable.txt)
  curl -sSLO "https://dl.k8s.io/release/${KUBE_VER}/bin/linux/amd64/kubectl"
  install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
  rm -f kubectl
else
  echo "kubectl already installed: $(kubectl version --client --short 2>/dev/null || kubectl version --client)"
fi

# helm
if ! command -v helm &>/dev/null; then
  echo "Installing Helm..."
  curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
else
  echo "Helm already installed: $(helm version --short)"
fi

# git, python3 (for local testing)
apt-get update -qq && apt-get install -y -qq git python3-pip python3-venv 2>/dev/null || true

# ── Create k3d cluster ─────────────────────────────────────────────────────────
CLUSTER_NAME="${CLUSTER_NAME:-o11y-demo}"

if k3d cluster list | grep -q "^${CLUSTER_NAME}"; then
  echo "k3d cluster '${CLUSTER_NAME}' already exists — skipping creation"
else
  echo "Creating k3d cluster '${CLUSTER_NAME}'..."
  k3d cluster create "${CLUSTER_NAME}" \
    --servers 1 \
    --agents 2 \
    --port "8080:80@loadbalancer" \
    --port "4317:4317@loadbalancer" \
    --port "4318:4318@loadbalancer" \
    --wait
fi

kubectl config use-context "k3d-${CLUSTER_NAME}"
echo "Cluster ready: $(kubectl cluster-info | head -1)"

# ── Helm repos ─────────────────────────────────────────────────────────────────
echo "Adding Helm repos..."
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>/dev/null || true
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart 2>/dev/null || true
helm repo update
echo "Step 1 complete."
