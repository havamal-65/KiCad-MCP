#!/usr/bin/env bash
# Run kicad-mcp inside an isolated virtual environment.
#
# On first run, creates .venv at the project root and installs kicad-mcp
# into it so it never touches the global Python environment.
# On subsequent runs, the venv is reused and startup is immediate.
# All arguments are forwarded to kicad-mcp.
#
# Usage:
#   ./run.sh
#   ./run.sh --transport sse --port 8765
#   ./run.sh --check

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

ensure_venv() {
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "[kicad-mcp] Creating virtual environment at $VENV_DIR ..." >&2
        python3 -m venv "$VENV_DIR"
    fi

    if ! "$VENV_PYTHON" -c "import kicad_mcp" 2>/dev/null; then
        echo "[kicad-mcp] Installing kicad-mcp into venv..." >&2
        "$VENV_PYTHON" -m pip install -e "$PROJECT_DIR" --quiet
    fi
}

ensure_venv

exec "$VENV_PYTHON" -m kicad_mcp "$@"
