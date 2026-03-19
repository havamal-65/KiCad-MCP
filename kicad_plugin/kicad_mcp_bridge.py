"""KiCad MCP Bridge Plugin.

This plugin runs inside KiCad's embedded Python interpreter and exposes a
local TCP server on localhost:9760 (configurable via KICAD_MCP_PLUGIN_PORT).

The MCP PluginBackend connects to this server to read live board data directly
from pcbnew without file-parsing or gRPC overhead.

Installation (Windows):
    Copy to %APPDATA%\\kicad\\9.0\\scripting\\plugins\\
    Restart KiCad — server starts automatically at plugin load time.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
from typing import Any

logger = logging.getLogger(__name__)

_PORT = int(os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760"))
_server_thread: threading.Thread | None = None
_tcp_server: socketserver.TCPServer | None = None


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

def _handle_ping() -> dict[str, Any]:
    try:
        import pcbnew
        version = pcbnew.GetBuildVersion()
    except Exception:
        version = "unknown"
    return {"pong": True, "kicad_version": version}


def _get_open_board(path: str):
    """Return the currently open pcbnew Board, verifying it matches *path*."""
    import pcbnew
    board = pcbnew.GetBoard()
    if board is None:
        raise ValueError("No board is currently open in KiCad")
    board_path = board.GetFileName()
    # Normalise separators for comparison
    if os.path.normcase(os.path.normpath(board_path)) != os.path.normcase(os.path.normpath(path)):
        raise ValueError(
            f"Requested board '{path}' does not match open board '{board_path}'. "
            "Open the correct .kicad_pcb file in KiCad first."
        )
    return board


def _handle_get_board_info(path: str) -> dict[str, Any]:
    import pcbnew
    board = _get_open_board(path)
    title_block = board.GetTitleBlock()
    bb = board.GetBoardEdgesBoundingBox()
    # Convert nm → mm
    width_mm = pcbnew.ToMM(bb.GetWidth())
    height_mm = pcbnew.ToMM(bb.GetHeight())
    return {
        "title": title_block.GetTitle(),
        "revision": title_block.GetRevision(),
        "layer_count": board.GetCopperLayerCount(),
        "width_mm": round(width_mm, 4),
        "height_mm": round(height_mm, 4),
        "net_count": board.GetNetCount(),
        "footprint_count": len(list(board.GetFootprints())),
    }


def _handle_get_components(path: str) -> list[dict[str, Any]]:
    import pcbnew
    board = _get_open_board(path)
    components = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        components.append({
            "reference": fp.GetReference(),
            "value": fp.GetValue(),
            "x": round(pcbnew.ToMM(pos.x), 4),
            "y": round(pcbnew.ToMM(pos.y), 4),
            "layer": fp.GetLayerName(),
            "rotation": round(fp.GetOrientationDegrees(), 4),
        })
    return components


def _handle_get_nets(path: str) -> list[dict[str, Any]]:
    board = _get_open_board(path)
    nets = []
    net_info = board.GetNetInfo()
    for net_id, net in net_info.NetsByNetcode().items():
        nets.append({
            "net_id": net_id,
            "name": net.GetNetname(),
        })
    return nets


# ---------------------------------------------------------------------------
# TCP request handler
# ---------------------------------------------------------------------------

class _MCPRequestHandler(socketserver.StreamRequestHandler):
    """Handles one newline-delimited JSON request/response cycle."""

    def handle(self):
        try:
            raw = self.rfile.readline()
            if not raw:
                return
            request = json.loads(raw.decode("utf-8").strip())
            response = self._dispatch(request)
        except Exception as exc:
            response = {"status": "error", "message": str(exc)}

        try:
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass  # client disconnected

    def _dispatch(self, request: dict) -> dict:
        method = request.get("method", "")
        path = request.get("path", "")

        try:
            if method == "ping":
                result = _handle_ping()
            elif method == "get_board_info":
                result = _handle_get_board_info(path)
            elif method == "get_components":
                result = _handle_get_components(path)
            elif method == "get_nets":
                result = _handle_get_nets(path)
            else:
                return {"status": "error", "message": f"Unknown method: {method!r}"}
            return {"status": "ok", "result": result}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _start_server() -> None:
    """Start the TCP server in a daemon background thread."""
    global _tcp_server, _server_thread

    try:
        socketserver.TCPServer.allow_reuse_address = True
        _tcp_server = socketserver.TCPServer(("localhost", _PORT), _MCPRequestHandler)
    except OSError as exc:
        logger.warning("KiCad MCP bridge: could not bind to port %d: %s", _PORT, exc)
        return

    _server_thread = threading.Thread(
        target=_tcp_server.serve_forever,
        name="kicad-mcp-bridge",
        daemon=True,
    )
    _server_thread.start()
    logger.info("KiCad MCP bridge listening on localhost:%d", _PORT)


# ---------------------------------------------------------------------------
# KiCad ActionPlugin interface
# ---------------------------------------------------------------------------

try:
    import pcbnew

    class KiCadMCPBridge(pcbnew.ActionPlugin):
        """ActionPlugin wrapper — server starts at load time, not on button press."""

        def defaults(self):
            self.name = "KiCad MCP Bridge"
            self.category = "MCP"
            self.description = (
                "Exposes a local TCP API so the KiCad MCP server can read "
                "live board data directly from pcbnew."
            )
            self.show_toolbar_button = False

        def Run(self):
            # No-op: the server is already running (started at module load).
            pass

    def register():
        """Called by KiCad when it loads this plugin file."""
        _start_server()
        plugin = KiCadMCPBridge()
        plugin.register()

except ImportError:
    # pcbnew not available (e.g., running tests outside KiCad)
    def register():
        _start_server()
