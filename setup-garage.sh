#!/usr/bin/env bash
set -euo pipefail

COMPOSE_SERVICE="garage"
KEY_NAME="openclaw-key"
BUCKET_NAME="openclaw"
ENV_FILE="$(dirname "$0")/.env"

garage() {
    docker compose exec "$COMPOSE_SERVICE" /garage "$@"
}

echo "==> Waiting for Garage to be ready..."
until docker compose exec "$COMPOSE_SERVICE" /garage status &>/dev/null; do
    sleep 1
done

echo "==> Fetching node ID..."
NODE_ID=$(garage node id 2>/dev/null | awk -F'@' 'NR==1 {print $1}')
if [[ -z "$NODE_ID" ]]; then
    echo "ERROR: could not determine node ID" >&2
    exit 1
fi
echo "    Node: $NODE_ID"

echo "==> Assigning layout (skip if already applied)..."
if garage status 2>&1 | grep -q "$NODE_ID"; then
    echo "    Layout already applied, skipping."
else
    CURRENT_VERSION=$(garage layout show 2>/dev/null | grep -oP 'Current cluster layout version: \K[0-9]+' || echo "0")
    NEXT_VERSION=$(( CURRENT_VERSION + 1 ))
    garage layout assign -z dc1 -c 1G "$NODE_ID"
    garage layout apply --version "$NEXT_VERSION"
fi

echo "==> Creating key '$KEY_NAME'..."
if garage key list | grep -q "$KEY_NAME"; then
    echo "    Key already exists, skipping creation."
else
    garage key create "$KEY_NAME"
fi

KEY_INFO=$(garage key info --show-secret "$KEY_NAME")
KEY_ID=$(echo "$KEY_INFO"     | grep "Key ID:"     | awk '{print $NF}')
KEY_SECRET=$(echo "$KEY_INFO" | grep "Secret key:" | awk '{print $NF}')

if [[ -z "$KEY_ID" || -z "$KEY_SECRET" ]]; then
    echo "ERROR: could not parse key credentials from garage output:" >&2
    echo "$KEY_INFO" >&2
    exit 1
fi

echo "==> Creating bucket '$BUCKET_NAME'..."
if garage bucket list | grep -q "^$BUCKET_NAME"; then
    echo "    Bucket already exists, skipping creation."
else
    garage bucket create "$BUCKET_NAME"
fi

echo "==> Granting key access to bucket..."
garage bucket allow "$BUCKET_NAME" --read --write --key "$KEY_NAME"

echo "==> Updating $ENV_FILE..."
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

sed -i "s|^S3_ACCESS_KEY_ID=.*|S3_ACCESS_KEY_ID=$KEY_ID|" "$ENV_FILE"
sed -i "s|^S3_SECRET_ACCESS_KEY=.*|S3_SECRET_ACCESS_KEY=$KEY_SECRET|" "$ENV_FILE"

echo ""
echo "Done."
echo "  Key ID:     $KEY_ID"
echo "  Key secret: $KEY_SECRET"
