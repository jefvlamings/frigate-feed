#!/usr/bin/env bash
# Install the official Frigate integration into the dev Home Assistant config.
#
# Shallow-clones blakeblackshear/frigate-hass-integration at a pinned tag and copies
# custom_components/frigate into config/custom_components/frigate (gitignored).
# Restart the dev HA afterwards:
#   docker compose -p frigate-feed-dev restart homeassistant
#
# Usage: ./scripts/install-frigate-integration.sh [tag]   (default: v5.15.6)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-v5.15.6}"
REPO_URL="https://github.com/blakeblackshear/frigate-hass-integration"
DEST="${ROOT_DIR}/config/custom_components/frigate"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "Cloning ${REPO_URL} @ ${TAG}"
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${TAG}" "${REPO_URL}" "${TMP_DIR}/src"

if [[ ! -f "${TMP_DIR}/src/custom_components/frigate/manifest.json" ]]; then
  echo "ERROR: custom_components/frigate not found in ${TAG}" >&2
  exit 1
fi

mkdir -p "${ROOT_DIR}/config/custom_components"
rm -rf "${DEST}"
cp -R "${TMP_DIR}/src/custom_components/frigate" "${DEST}"

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${DEST}/manifest.json" 2>/dev/null || echo "?")"
echo "Installed Frigate integration ${version} into config/custom_components/frigate"
