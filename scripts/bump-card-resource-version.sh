#!/usr/bin/env bash
set -euo pipefail

SOURCE_FILE="${3:-custom_components/frigate_feed/frontend/frigate-feed-card.js}"
TARGET_FILE="${4:-config/www/frigate-feed-card.js}"
FILE="${1:-config/.storage/lovelace_resources}"
CARD_PATH="${2:-/local/frigate-feed-card.js}"

if [ ! -f "$SOURCE_FILE" ]; then
  echo "Card source not found: $SOURCE_FILE" >&2
  exit 1
fi

if [ ! -f "$FILE" ]; then
  echo "File not found: $FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET_FILE")"
cp "$SOURCE_FILE" "$TARGET_FILE"
echo "Synced card: $SOURCE_FILE -> $TARGET_FILE"

node - "$FILE" "$CARD_PATH" <<'NODE'
const fs = require("fs");
const crypto = require("crypto");

const file = process.argv[2];
const cardPath = process.argv[3];
const integrationCardPath = "/frigate_feed/frigate-feed-card.js";
const epoch = Math.floor(Date.now() / 1000);
const raw = fs.readFileSync(file, "utf8");
const json = JSON.parse(raw);
const items = json?.data?.items;

if (!Array.isArray(items)) {
  console.error("Invalid lovelace_resources format");
  process.exit(1);
}

const baseUrl = (url) => String(url || "").split("?", 1)[0];

const filtered = [];
for (const item of items) {
  if (!item || typeof item.url !== "string") continue;
  const base = baseUrl(item.url);
  if (base === integrationCardPath) {
    console.log(`Removed conflicting resource URL: ${item.url}`);
    continue;
  }
  filtered.push(item);
}
json.data.items = filtered;

let updated = false;
let localItem = null;
const deduped = [];
for (const item of filtered) {
  if (baseUrl(item.url) !== cardPath) {
    deduped.push(item);
    continue;
  }
  if (localItem === null) {
    localItem = item;
    deduped.push(item);
    continue;
  }
  console.log(`Removed duplicate local resource URL: ${item.url}`);
}
json.data.items = deduped;

if (localItem === null) {
  localItem = {
    id: crypto.randomUUID(),
    type: "module",
    url: `${cardPath}?v=${epoch}`,
  };
  json.data.items.push(localItem);
  console.log(`Added resource URL: ${localItem.url}`);
  updated = true;
} else {
  const [base, query = ""] = localItem.url.split("?");
  const params = new URLSearchParams(query);
  params.set("v", String(epoch));
  localItem.url = `${base}?${params.toString()}`;
  console.log(`Updated resource URL to: ${localItem.url}`);
  updated = true;
}

if (!updated) {
  console.error(`No matching resource found for ${cardPath}`);
  process.exit(1);
}

fs.writeFileSync(file, `${JSON.stringify(json, null, 2)}\n`);
NODE

echo "Done. Reload Home Assistant UI."
echo "Restarting dev Home Assistant container (compose project frigate-feed-dev)..."
docker compose -p frigate-feed-dev restart homeassistant
echo "Done. Dev Home Assistant container restarted."
