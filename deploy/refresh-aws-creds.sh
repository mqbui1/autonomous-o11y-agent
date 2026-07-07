#!/bin/bash
# Refresh AWS credentials from dev-login and write to ~/.aws/credentials.
# Containers mount ~/.aws/credentials as a read-only bind mount, so they
# pick up the new token immediately — no container restart needed.
#
# Usage:
#   ./deploy/refresh-aws-creds.sh
#
# To auto-refresh hourly via crontab:
#   0 * * * * /path/to/autonomous-o11y-agent/deploy/refresh-aws-creds.sh >> /tmp/aws-refresh.log 2>&1

set -e

PROFILE="387769110234_bedrock-inference-role"
CREDS_FILE="$HOME/.aws/credentials"

echo "Fetching credentials for profile: $PROFILE ..."
RAW=$(dev-login aws credential-process --profile "$PROFILE")

KEY=$(echo "$RAW"    | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
SECRET=$(echo "$RAW" | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
TOKEN=$(echo "$RAW"  | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")
EXPIRY=$(echo "$RAW" | python3 -c "import sys,json; print(json.load(sys.stdin)['Expiration'])")

# Write both [default] and the named profile so containers work with or without AWS_PROFILE set
cat > "$CREDS_FILE" <<EOF
[default]
aws_access_key_id = $KEY
aws_secret_access_key = $SECRET
aws_session_token = $TOKEN

[$PROFILE]
aws_access_key_id = $KEY
aws_secret_access_key = $SECRET
aws_session_token = $TOKEN
EOF

chmod 600 "$CREDS_FILE"
echo "Credentials written to $CREDS_FILE"
echo "Valid until: $EXPIRY"
echo "Containers using the bind mount will use the new token on their next AWS call."
