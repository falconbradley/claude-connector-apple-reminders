#!/usr/bin/env bash
# build.sh — validate and pack the Apple Reminders MCP desktop extension
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SCRIPT_DIR}/dist"

echo "=== Apple Reminders MCP — build ==="

# Check for mcpb CLI
if ! command -v mcpb &>/dev/null; then
    echo "Installing mcpb CLI…"
    npm install -g @anthropic-ai/mcpb
fi

# Validate
echo ""
echo "Validating manifest…"
mcpb validate "${SCRIPT_DIR}/manifest.json"
echo "✓ Manifest valid."

# Pack
echo ""
mkdir -p "${OUT}"
mcpb pack "${SCRIPT_DIR}" "${OUT}/apple-reminders.mcpb"

echo ""
echo "✓ Built: ${OUT}/apple-reminders.mcpb"
echo ""
echo "To install: double-click the .mcpb file, or drag it into Claude Desktop."
echo ""
echo "ℹ  macOS will prompt for Reminders access on first use. You can also pre-grant"
echo "   under System Settings → Privacy & Security → Reminders."
