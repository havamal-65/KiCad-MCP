#!/usr/bin/env bash
# Run kicad-mcp-plugin inside an isolated virtual environment.
#
# Requires KiCad to be open with kicad_mcp_bridge installed and enabled.
# Hard-fails at startup if the bridge TCP server is not reachable on localhost:9760.
#
# On first run, creates .venv at the project root and installs kicad-mcp
# into it so it never touches the global Python environment.
# On subsequent runs, the venv is reused and startup is immediate.
# All arguments are forwarded to kicad-mcp-plugin.
#
# Usage:
#   ./run_plugin.sh
#   ./run_plugin.sh --transport sse --port 8765

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

# Ignore global Python overrides so the venv's pinned dependencies win.
unset PYTHONHOME || true
unset PYTHONPATH || true
export FASTMCP_LOG_ENABLED="${FASTMCP_LOG_ENABLED:-false}"

ensure_venv() {
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "[kicad-mcp-plugin] Creating virtual environment at $VENV_DIR ..." >&2
        python3 -m venv "$VENV_DIR"
    fi

    if ! "$VENV_PYTHON" -c "import kicad_mcp_plugin" 2>/dev/null; then
        echo "[kicad-mcp-plugin] Installing kicad-mcp into venv..." >&2
        "$VENV_PYTHON" -m pip install -e "$PROJECT_DIR" --quiet
    fi
}

ensure_venv

exec "$VENV_PYTHON" -m kicad_mcp_plugin "$@"
