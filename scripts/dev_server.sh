#!/usr/bin/env bash
# Dev hot-reload MCP server (streamable-http) for KiCad-MCP.
#
# Runs kicad_mcp_plugin under watchfiles so any edit under src/ auto-restarts
# the server. Claude Code — pointed at .mcp.dev.json — auto-reconnects on the
# next tool call (HTTP transport, exponential backoff). No manual /mcp.
#
# End users are unaffected: the committed .mcp.json still uses stdio.
#
# Usage:
#   scripts/dev_server.sh [PORT] [HOST]
# Then, in another terminal:
#   claude --strict-mcp-config --mcp-config .mcp.dev.json
set -euo pipefail

PORT="${1:-8765}"
HOST="${2:-127.0.0.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/Scripts/python.exe"   # Windows venv under git-bash
[ -x "$PY" ] || { echo "venv python not found under $ROOT/.venv" >&2; exit 1; }

echo "Dev MCP server (hot-reload): http://$HOST:$PORT/mcp"
echo "Watching $ROOT/src — edit & save to auto-restart. Ctrl+C to stop."
echo "Point Claude at it: claude --strict-mcp-config --mcp-config .mcp.dev.json"

exec "$PY" -m watchfiles \
  "$PY -m kicad_mcp_plugin --transport streamable-http --host $HOST --port $PORT" \
  "$ROOT/src"
