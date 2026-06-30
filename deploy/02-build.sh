#!/usr/bin/env bash
# Build the o11y-agent Docker image and load it into k3d.
# Strategy:
#   1. Try to pull pre-built image from GHCR (fast, no CPU spike)
#   2. Fall back to local build if pull fails
# Build context is the PARENT directory so all sibling projects are available.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"
AGENT_IMAGE="${AGENT_IMAGE:-o11y-agent:latest}"
CLUSTER_NAME="${CLUSTER_NAME:-o11y-demo}"
GHCR_IMAGE="ghcr.io/mqbui1/o11y-agent:latest"

echo ""
echo "── Step 2: Build/pull o11y-agent image ───────────────────────────────"

# ── Attempt 1: pull from GHCR ─────────────────────────────────────────────
if docker pull "${GHCR_IMAGE}" 2>/dev/null; then
  echo "Pulled from GHCR: ${GHCR_IMAGE}"
  docker tag "${GHCR_IMAGE}" "${AGENT_IMAGE}"
else
  echo "GHCR pull failed (image not yet published or private) — building locally..."

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
      echo "  Cloning https://github.com/mqbui1/$m ..."
      git clone --depth=1 "https://github.com/mqbui1/$m" "$PARENT_DIR/$m" || echo "  WARNING: clone failed for $m"
    done
  fi

  echo "Building from context: $PARENT_DIR"
  echo "Dockerfile: $REPO_DIR/Dockerfile"

  DOCKER_BUILDKIT=0 docker build \
    -t "${AGENT_IMAGE}" \
    -f "$REPO_DIR/Dockerfile" \
    "$PARENT_DIR"
fi

echo "Loading image into k3d cluster '${CLUSTER_NAME}'..."
k3d image import "${AGENT_IMAGE}" -c "${CLUSTER_NAME}"

echo "Image loaded: ${AGENT_IMAGE}"
echo "Step 2 complete."
