#!/usr/bin/env bash
# Install the KiCad MCP bridge plugin into KiCad 9 on Linux or macOS.
#
# SYNOPSIS
#   ./install_bridge.sh [--version 9.0]
#
# DESCRIPTION
#   Copies kicad_mcp_bridge.py as __init__.py into the KiCad PCM plugins
#   directory and patches pcbnew.json so KiCad auto-loads the bridge on
#   every pcbnew startup without needing a manual "Refresh Plugins" step.
#
#   Also removes any stale copies from scripting/plugins/ — those trigger a
#   sys.modules conflict that silently prevents the bridge from starting
#   (same root cause as the Windows bug; see Lesson 37 in LESSONS_LEARNED.md).
#
# REQUIREMENTS
#   bash, python3 (for JSON patching)
#
# USAGE
#   chmod +x kicad_plugin/install_bridge.sh
#   ./kicad_plugin/install_bridge.sh
#
# LINUX PATHS
#   Plugins: $XDG_DATA_HOME/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/
#   Config:  $XDG_CONFIG_HOME/kicad/9.0/pcbnew.json
#
# MACOS PATHS
#   Plugins: ~/Library/Preferences/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/
#   Config:  ~/Library/Preferences/kicad/9.0/pcbnew.json

set -euo pipefail

KICAD_VERSION="9.0"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            KICAD_VERSION="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Locate source
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="$SCRIPT_DIR/kicad_mcp_bridge.py"

if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "ERROR: Source not found: $SOURCE_FILE" >&2
    echo "Run this script from the repo root or the kicad_plugin/ directory." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Detect OS and resolve KiCad user directories
# ---------------------------------------------------------------------------

OS="$(uname -s)"

if [[ "$OS" == "Darwin" ]]; then
    # macOS: KiCad 9 stores user data and config together
    KICAD_DATA="$HOME/Library/Preferences/kicad/$KICAD_VERSION"
    KICAD_CONFIG="$HOME/Library/Preferences/kicad/$KICAD_VERSION"
else
    # Linux: XDG Base Directory spec
    XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
    KICAD_DATA="$XDG_DATA_HOME/kicad/$KICAD_VERSION"
    KICAD_CONFIG="$XDG_CONFIG_HOME/kicad/$KICAD_VERSION"
fi

PLUGINS_ROOT="$KICAD_DATA/3rdparty/plugins"
BRIDGE_DIR="$PLUGINS_ROOT/kicad_mcp_bridge"
TARGET_FILE="$BRIDGE_DIR/__init__.py"
PCBNEW_JSON="$KICAD_CONFIG/pcbnew.json"
SCRIPTING_DIR="$KICAD_DATA/scripting/plugins"

echo "OS               : $OS"
echo "KiCad version    : $KICAD_VERSION"
echo "Plugin data dir  : $KICAD_DATA"
echo "Config dir       : $KICAD_CONFIG"
echo ""

# ---------------------------------------------------------------------------
# Step 1 — Remove stale scripting/plugins copies (sys.modules conflict fix)
#
# KiCad loads scripting/plugins early, before pcbnew is fully available.
# A stale bridge there gets cached in sys.modules in a broken state and
# prevents the real 3rdparty/plugins copy from initialising correctly.
# ---------------------------------------------------------------------------

STALE_FILE="$SCRIPTING_DIR/kicad_mcp_bridge.py"
if [[ -f "$STALE_FILE" ]]; then
    rm -f "$STALE_FILE"
    echo "Deleted stale scripting plugin file: $STALE_FILE"
fi

STALE_DIR="$SCRIPTING_DIR/kicad_mcp_bridge"
if [[ -d "$STALE_DIR" ]]; then
    rm -rf "$STALE_DIR"
    echo "Deleted stale scripting plugin directory: $STALE_DIR"
fi

# ---------------------------------------------------------------------------
# Step 2 — Install bridge as a PCM plugin package
# ---------------------------------------------------------------------------

mkdir -p "$BRIDGE_DIR"
cp "$SOURCE_FILE" "$TARGET_FILE"
echo "Installed: $TARGET_FILE"

# ---------------------------------------------------------------------------
# Step 3 — Patch pcbnew.json so KiCad records the plugin in action_plugins
# ---------------------------------------------------------------------------

python3 - <<PYEOF
import json, os, sys

pcbnew_json = "$PCBNEW_JSON"
plugin_path = "$BRIDGE_DIR"

if os.path.exists(pcbnew_json):
    with open(pcbnew_json, "r", encoding="utf-8") as f:
        cfg = json.load(f)
else:
    print(f"pcbnew.json not found — will create: {pcbnew_json}")
    os.makedirs(os.path.dirname(pcbnew_json), exist_ok=True)
    cfg = {}

plugins = cfg.get("action_plugins", [])
already_present = any(p.get("path") == plugin_path for p in plugins)

if already_present:
    print("action_plugins entry already present in pcbnew.json")
else:
    plugins.append({"path": plugin_path, "show_button": False})
    cfg["action_plugins"] = plugins
    with open(pcbnew_json, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"Patched pcbnew.json with action_plugins entry")
PYEOF

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

PORT="${KICAD_MCP_PLUGIN_PORT:-9760}"

echo ""
echo "Installation complete."
echo "  Plugin directory : $BRIDGE_DIR"
echo "  Entry point      : $TARGET_FILE"
echo "  Port             : $PORT"
echo ""
echo "Next steps:"
echo "  1. Close all KiCad / pcbnew windows."
echo "  2. Open pcbnew and load any board."
echo "  3. Verify bridge is running:"
echo "       python3 -c \"import socket; s=socket.create_connection(('localhost',$PORT),2); print('bridge OK'); s.close()\""
echo "  4. Start MCP server:"
echo "       python -m kicad_mcp_plugin"
echo ""
echo "To reinstall after a source update:"
echo "  bash kicad_plugin/install_bridge.sh"
echo "  (then close and reopen pcbnew)"
