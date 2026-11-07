#!/usr/bin/env bash
# Register Ollama Cursor Agent templates as Unraid Private Apps (Community Applications).
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid}"
DEST="${DEST:-/boot/config/plugins/community.applications/private/ollama-cursor-agent}"

if [[ ! -d /boot/config ]]; then
  echo "This script is meant to run on Unraid (expected /boot/config)." >&2
  exit 1
fi

mkdir -p "$DEST"
curl -fsSL "$REPO_RAW/ollama.xml" -o "$DEST/ollama.xml"
curl -fsSL "$REPO_RAW/ollama-cursor-agent.xml" -o "$DEST/ollama-cursor-agent.xml"

TUSER="${TUSER:-/boot/config/plugins/dockerMan/templates-user}"
if [[ -d "$TUSER" ]]; then
  cp -f "$DEST/ollama.xml" "$TUSER/my-ollama.xml"
  cp -f "$DEST/ollama-cursor-agent.xml" "$TUSER/my-ollama-cursor-agent.xml"
  echo "Also copied to Docker templates-user: $TUSER"
fi

# Legacy path from an older template pack name
LEGACY="${LEGACY_DEST:-/boot/config/plugins/community.applications/private/cellar-loader}"
if [[ -d "$LEGACY" && "$LEGACY" != "$DEST" ]]; then
  cp -f "$DEST/ollama.xml" "$LEGACY/ollama.xml" 2>/dev/null || true
  cp -f "$DEST/ollama-cursor-agent.xml" "$LEGACY/ollama-cursor-agent.xml" 2>/dev/null || true
  echo "Also refreshed legacy Private Apps folder: $LEGACY"
fi

echo "Templates installed under: $DEST"
echo "Open Apps → Private Apps → Actions → Update (or reinstall) on ollama-cursor-agent."
echo "Image is published as ghcr.io/killamfkr/ollama-cursor-agent:latest — pull manually if Docker does not show an update."
