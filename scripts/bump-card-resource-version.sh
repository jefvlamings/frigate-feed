#!/usr/bin/env bash
# Bust the browser cache for the bundled card in the dev Home Assistant.
#
# The integration registers the card as /frigate_feed/frigate-feed-card.js?v=<version>, so
# card edits only reach browsers when the `v` tag changes. This sets it to
# `<version>-dev<epoch>` through the Lovelace resources WebSocket API (no file edits under
# config/.storage, which the container owns, and no restart). The integration resets the
# tag to `<version>` on its next start, which serves the same file.
#
# Usage: HA_TOKEN=<long-lived access token> ./scripts/bump-card-resource-version.sh [ha_url]
set -euo pipefail

HA_URL="${1:-${HA_URL:-http://localhost:8124}}"
if [ -z "${HA_TOKEN:-}" ]; then
  echo "Set HA_TOKEN to a long-lived access token of an admin user (Profile > Security)." >&2
  exit 1
fi

VERSION="$(node -p 'require("./custom_components/frigate_feed/manifest.json").version')"

HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" VERSION="$VERSION" node - <<'NODE'
const CARD_PATH = "/frigate_feed/frigate-feed-card.js";
const wsUrl = `${process.env.HA_URL.replace(/^http/, "ws").replace(/\/$/, "")}/api/websocket`;
const url = `${CARD_PATH}?v=${process.env.VERSION}-dev${Math.floor(Date.now() / 1000)}`;

const ws = new WebSocket(wsUrl);
const fail = (message) => {
  console.error(message);
  process.exit(1);
};
ws.onerror = () => fail(`Cannot connect to ${wsUrl}`);
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "auth_required") {
    ws.send(JSON.stringify({ type: "auth", access_token: process.env.HA_TOKEN }));
  } else if (msg.type === "auth_invalid") {
    fail("Authentication failed: check HA_TOKEN");
  } else if (msg.type === "auth_ok") {
    ws.send(JSON.stringify({ id: 1, type: "lovelace/resources" }));
  } else if (msg.id === 1) {
    if (!msg.success) fail(`Listing resources failed: ${JSON.stringify(msg.error)}`);
    const item = msg.result.find((res) => String(res.url).split("?", 1)[0] === CARD_PATH);
    const request = item
      ? { id: 2, type: "lovelace/resources/update", resource_id: item.id, res_type: "module", url }
      : { id: 2, type: "lovelace/resources/create", res_type: "module", url };
    ws.send(JSON.stringify(request));
  } else if (msg.id === 2) {
    if (!msg.success) fail(`Updating the resource failed: ${JSON.stringify(msg.error)}`);
    console.log(`Card resource: ${msg.result.url}`);
    console.log("Done. Reload the Home Assistant UI.");
    ws.close();
    process.exit(0);
  }
};
NODE
