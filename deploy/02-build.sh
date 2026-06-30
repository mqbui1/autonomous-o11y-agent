#!/usr/bin/env bash
# Build the o11y-agent Docker image and load it into k3d.
# Build context is the PARENT directory so all sibling projects are available.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"
AGENT_IMAGE="${AGENT_IMAGE:-o11y-agent:latest}"
CLUSTER_NAME="${CLUSTER_NAME:-o11y-demo}"

echo ""
echo "── Step 2: Build o11y-agent image ────────────────────────────────────────"

# Verify sibling projects exist
REQUIRED_SIBLINGS=(
  "auto-detector-provisioner"
  "o11y-usage-governance"
  "o11y-instrumentation-analyzer"
  "splunk-o11y-health-check"
)

MISSING=()
for sibling in "${REQUIRED_SIBLINGS[@]}"; do
  if [ ! -d "$PARENT_DIR/$sibling" ]; then
    MISSING+=("$sibling")
  fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "ERROR: Missing sibling repositories in $PARENT_DIR:"
  for m in "${MISSING[@]}"; do
    echo "  - $m"
  done
  echo ""
  echo "Clone them with:"
  echo "  cd $PARENT_DIR"
  echo "  git clone https://github.com/mqbui1/auto-detector-provisioner"
  echo "  git clone https://github.com/mqbui1/o11y-usage-governance"
  echo "  git clone https://github.com/mqbui1/o11y-instrumentation-analyzer"
  echo "  git clone https://github.com/mqbui1/splunk-o11y-health-check"
  exit 1
fi

echo "Building from context: $PARENT_DIR"
echo "Dockerfile: $REPO_DIR/Dockerfile"

docker build \
  -t "${AGENT_IMAGE}" \
  -f "$REPO_DIR/Dockerfile" \
  "$PARENT_DIR"

echo "Loading image into k3d cluster '${CLUSTER_NAME}'..."
k3d image import "${AGENT_IMAGE}" -c "${CLUSTER_NAME}"

echo "Image loaded: ${AGENT_IMAGE}"
echo "Step 2 complete."
