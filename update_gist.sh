#!/bin/bash
# Waits for tunnel URL, updates GitHub Gist, and notifies Discord
GIST_ID="723f7ea98c33725338a6003bd14765c4"
LOG="/tmp/qse_tunnel.log"
LAST_URL_FILE="/tmp/qse_last_tunnel_url"
ENV_FILE="$(dirname "$0")/.env"

# Load credentials
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

if [ -z "$GIST_GITHUB_TOKEN" ] || [ -z "$DISCORD_WEBHOOK" ]; then
    echo "[update_gist] Missing credentials in .env" >&2
    exit 1
fi

# Wait up to 30s for the tunnel URL to appear
for i in $(seq 1 30); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$URL" ]; then
    echo "[update_gist] No tunnel URL found" >&2
    exit 1
fi

# Skip if URL hasn't changed
LAST_URL=$(cat "$LAST_URL_FILE" 2>/dev/null)
if [ "$URL" = "$LAST_URL" ]; then
    echo "[update_gist] URL unchanged: $URL"
    exit 0
fi

# Update Gist
curl -s -X PATCH "https://api.github.com/gists/$GIST_ID" \
    -H "Authorization: token $GIST_GITHUB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"files\":{\"daleel.json\":{\"content\":\"{\\\"url\\\": \\\"$URL\\\"}\"}}}" \
    > /dev/null

echo "[update_gist] Updated Gist with $URL"
echo "$URL" > "$LAST_URL_FILE"

# Notify Discord via webhook (instant)
curl -s -X POST "$DISCORD_WEBHOOK" \
    -H "Content-Type: application/json" \
    -d "{\"content\": \"🔗 **Daleel** is online at a new URL:\\n$URL\"}" \
    > /dev/null

echo "[update_gist] Discord notification sent"
