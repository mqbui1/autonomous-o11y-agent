#!/bin/bash
# Refresh AWS Bedrock session credentials in .env (tokens expire ~1h).
# Run this when the agent starts failing LLM calls with auth errors.
#
# Usage: ./refresh-aws-creds.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo "Fetching fresh AWS credentials..."
eval $(aws configure export-credentials --format env)

if [ -z "$AWS_ACCESS_KEY_ID" ]; then
  echo "ERROR: Failed to get credentials. Is your Okta session active?"
  exit 1
fi

# Update .env in-place
sed -i '' "s|^AWS_ACCESS_KEY_ID=.*|AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID|" "$ENV_FILE"
sed -i '' "s|^AWS_SECRET_ACCESS_KEY=.*|AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY|" "$ENV_FILE"
sed -i '' "s|^AWS_SESSION_TOKEN=.*|AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN|" "$ENV_FILE"

echo "Updated .env with fresh credentials (key: ${AWS_ACCESS_KEY_ID:0:12}...)"
echo ""
echo "Recreating containers to pick up new creds..."
docker compose up -d o11y-agent supervisor
