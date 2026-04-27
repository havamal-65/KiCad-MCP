"""Plugin backend — communicates with the kicad_mcp_bridge KiCad plugin.

The bridge plugin runs inside KiCad's embedded Python interpreter and starts a
local TCP server on localhost:9760.  This backend connects to that server to
perform board operations directly via the pcbnew API — no gRPC, no file-parsing.

Capabilities
------------
Full board read+write surface (replaces IPC board ops on Windows):
  - get_board_info, get_components, get_nets, get_tracks
  - get_design_rules, get_stackup, get_active_project
  - place_component, move_component, add_track, add_via, assign_net, refill_zones

Schematic ops are NOT supported (KiCad 9 doesn't expose eeschema scripting);
those continue to use the file backend via CompositeBackend.

Environment variables
---------------------
KICAD_MCP_PLUGIN_PORT     TCP port (default 9760)
KICAD_MCP_PLUGIN_TIMEOUT  Ping / is_available timeout in seconds (default 2.0)
KICAD_MCP_PLUGIN_OP_TIMEOUT  Board op timeout in seconds (default 10.0)
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    KiCadBackend,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import BackendNotAvailableError

logger = get_logger("backend.plugin")


class BridgeTemporarilyUnavailableError(BackendNotAvailableError):
    """Raised when the kicad_mcp_bridge TCP server drops mid-session.

    Distinct from BridgeNotAvailableError (startup failure).  This error means
    KiCad crashed or was closed after the MCP server started.  The server catches
    this, marks the bridge as down, and returns a helpful message so the caller
    knows to reopen KiCad.
    """


_DEFAULT_PORT = 9760
_DEFAULT_PING_TIMEOUT = 2.0
_DEFAULT_OP_TIMEOUT = 10.0


def _get_port() -> int:
    return int(os.environ.get("KICAD_MCP_PLUGIN_PORT", str(_DEFAULT_PORT)))


def _get_ping_timeout() -> float:
    return float(os.environ.get("KICAD_MCP_PLUGIN_TIMEOUT", str(_DEFAULT_PING_TIMEOUT)))


def _get_op_timeout() -> float:
    return float(os.environ.get("KICAD_MCP_PLUGIN_OP_TIMEOUT", str(_DEFAULT_OP_TIMEOUT)))


# ---------------------------------------------------------------------------
# Low-level transport
# ---------------------------------------------------------------------------

def _tcp_call(method: str, timeout: float, **kwargs) -> Any:
    """Send one JSON request to the bridge and return the result payload.

    Raises:
        BridgeTemporarilyUnavailableError: Bridge not reachable (KiCad closed/crashed).
        RuntimeError: Bridge returned an error response.
    """
    port = _get_port()
    request = {"method": method, **kwargs}
    try:
        with socket.create_connection(("localhost", port), timeout=timeout) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            sock.settimeout(timeout)
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        raise BridgeTemporarilyUnavailableError(
            f"Bridge unreachable on port {port}: {exc}. "
            "KiCad may have closed or crashed. Reopen KiCad and enable kicad_mcp_bridge."
        ) from exc
    response = json.loads(data.decode("utf-8").strip())
    if response.get("status") == "error":
        raise RuntimeError(f"Plugin bridge error: {response.get('message', 'unknown')}")
    return response.get("result")


# ---------------------------------------------------------------------------
# Board ops
# ---------------------------------------------------------------------------

class PluginBoardOps(BoardOps):
    """Board operations via the kicad_mcp_bridge plugin TCP server."""

    # Optional callback invoked when the bridge drops mid-session.
    # Set by PluginDirectBackend to reset its _bridge_available flag.
    _on_disconnect: "Any | None" = None

    def _call(self, method: str, path: Path | str | None = None, **kwargs) -> Any:
        kw = kwargs
        if path is not None:
            kw = {"path": str(path), **kwargs}
        try:
            return _tcp_call(method, _get_op_timeout(), **kw)
        except BridgeTemporarilyUnavailableError:
            if self._on_disconnect is not None:
                self._on_disconnect()
            raise

    # -- Read ----------------------------------------------------------------

    def read_board(self, path: Path) -> dict[str, Any]:
        info = self.get_board_info(path)
        components = self.get_components(path)
        nets = self.get_nets(path)
        tracks = self.get_tracks(path)
        return {"info": info, "components": components, "nets": nets, "tracks": tracks}

    def get_board_info(self, path: Path) -> dict[str, Any]:
        return self._call("get_board_info", path)

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return self._call("get_components", path)

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return self._call("get_nets", path)

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        return self._call("get_tracks", path)

    def get_board_info_extended(self, path: Path) -> dict[str, Any]:
        return self._call("get_board_info", path)

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        return self._call("get_design_rules", path)

    def get_stackup(self, path: Path) -> dict[str, Any]:
        return self._call("get_stackup", path)

    # -- Write ---------------------------------------------------------------

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        return self._call("place_component", path,
                          reference=reference, footprint=footprint,
                          x=x, y=y, layer=layer, rotation=rotation)

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"reference": reference, "x": x, "y": y}
        if rotation is not None:
            kwargs["rotation"] = rotation
        return self._call("move_component", path, **kwargs)

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        return self._call("add_track", path,
                          start_x=start_x, start_y=start_y,
                          end_x=end_x, end_y=end_y,
                          width=width, layer=layer, net=net)

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        return self._call("add_via", path,
                          x=x, y=y, size=size, drill=drill,
                          net=net, via_type=via_type)

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        return self._call("assign_net", path,
                          reference=reference, pad=pad, net=net)

    def refill_zones(self, path: Path) -> dict[str, Any]:
        return self._call("refill_zones", path)

    def save_board(self, path: Path) -> dict[str, Any]:
        return self._call("save_board", path)

    def reload_board(self, path: Path) -> dict[str, Any]:
        return self._call("reload_board", path)

    def add_board_outline(
        self, path: Path, x: float, y: float,
        width: float, height: float, line_width: float = 0.05,
    ) -> dict[str, Any]:
        return self._call("add_board_outline", path,
                          x=x, y=y, width=width, height=height, line_width=line_width)

    def auto_place(
        self, path: Path, board_x: float, board_y: float,
        board_width: float, board_height: float, clearance_mm: float = 1.5,
    ) -> dict[str, Any]:
        return self._call("auto_place", path,
                          board_x=board_x, board_y=board_y,
                          board_width=board_width, board_height=board_height,
                          clearance_mm=clearance_mm)

    def place_components_bulk(
        self, path: Path, components: list[dict],
    ) -> dict[str, Any]:
        return self._call("place_components_bulk", path, components=components)

    def export_dsn(self, path: Path, dsn_path: Path) -> dict[str, Any]:
        return self._call("export_dsn", path, dsn_path=str(dsn_path))

    def import_ses(self, path: Path, ses_path: Path) -> dict[str, Any]:
        return self._call("import_ses", path, ses_path=str(ses_path))


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class PluginBackend(KiCadBackend):
    """KiCad backend that talks to the in-process kicad_mcp_bridge plugin."""

    name = "plugin"
    capabilities = {
        BackendCapability.BOARD_READ,
        BackendCapability.BOARD_MODIFY,
        BackendCapability.ZONE_REFILL,
        BackendCapability.BOARD_STACKUP,
        BackendCapability.BOARD_ROUTE,
    }

    # Availability cache (class-level so is_available() is cheap on repeated calls)
    _cache_result: bool | None = None
    _cache_ts: float = 0.0
    _CACHE_TTL: float = 5.0

    def is_available(self) -> bool:
        now = time.monotonic()
        if self._cache_result is not None and (now - self._cache_ts) < self._CACHE_TTL:
            return self._cache_result
        available = self._probe()
        self._cache_result = available
        self._cache_ts = now
        return available

    def _probe(self) -> bool:
        """Try a ping; return True if bridge responds correctly."""
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            return result.get("pong") is True
        except (ConnectionRefusedError, OSError, json.JSONDecodeError, TimeoutError):
            return False

    def get_version(self) -> str | None:
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            return result.get("kicad_version")
        except Exception:
            return None

    def get_board_ops(self) -> PluginBoardOps:
        return PluginBoardOps()

    def get_active_project(self) -> dict[str, Any]:
        try:
            return _tcp_call("get_active_project", _get_op_timeout())
        except Exception as exc:
            raise RuntimeError(f"Plugin bridge get_active_project failed: {exc}") from exc
