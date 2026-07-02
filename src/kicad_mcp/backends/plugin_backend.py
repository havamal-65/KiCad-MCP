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


class StaleBoardError(RuntimeError):
    """The bridge refused a mutation because the .kicad_pcb on disk is newer than
    the board it holds in memory (#14C / #8).

    Emitted by the bridge as a structured ``{error_code: "stale_board", ...}``
    response. ``PluginBoardOps._call`` self-heals by reloading the board from
    disk once and retrying the original op; a second stale verdict propagates.
    """

    def __init__(self, message: str, disk_mtime: float | None = None,
                 loaded_mtime: float | None = None) -> None:
        super().__init__(message)
        self.disk_mtime = disk_mtime
        self.loaded_mtime = loaded_mtime


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
        if response.get("error_code") == "stale_board":
            raise StaleBoardError(
                response.get("message", "board on disk is newer than in-memory copy"),
                response.get("disk_mtime"),
                response.get("loaded_mtime"),
            )
        raise RuntimeError(f"Plugin bridge error: {response.get('message', 'unknown')}")
    return response.get("result")


def _validate_bridge_identity(result: Any) -> None:
    """Reject a ping identity that belongs to a non-pcbnew owner (#13B).

    Post-#13B the bridge only binds inside pcbnew and reports ``app="pcbnew"``.
    If something else is holding the port — the classic case is the KiCad
    project manager leaking port 9760 across a pcbnew restart — the ping
    identifies it and we treat the bridge as unavailable so the existing
    fallback machinery engages.  We never auto-kill the offending process;
    the error just names it.

    Bridges from before the identity handshake omit the ``app`` field entirely;
    those are accepted unchanged (legacy back-compat).
    """
    if not isinstance(result, dict):
        return
    app = result.get("app")
    if app is None:
        return  # legacy bridge — no identity payload, accept as-is
    if app != "pcbnew":
        pid = result.get("pid")
        raise BridgeTemporarilyUnavailableError(
            f"Bridge on port {_get_port()} is held by {app!r}"
            + (f" (pid {pid})" if pid is not None else "")
            + ", not the pcbnew editor. This usually means the KiCad project "
            "manager is holding the port. Close all KiCad windows, then reopen "
            "the board in the PCB editor."
        )


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
        except StaleBoardError as stale:
            # Disk changed under the bridge (#14C): reload from disk once, then
            # retry the original op exactly once. A second stale verdict — the
            # file changed again between reload and retry — propagates.
            if path is None:
                raise
            try:
                reload_result = _tcp_call("reload_board", _get_op_timeout(), path=str(path))
            except BridgeTemporarilyUnavailableError:
                if self._on_disconnect is not None:
                    self._on_disconnect()
                raise
            # The bridge reports loaded=False when pcbnew could not reload the
            # board in place (KiCad 9 embedded board.Load() quirk). Retrying
            # would either re-stale or clobber the newer disk state, so refuse
            # with actionable guidance instead.
            if isinstance(reload_result, dict) and reload_result.get("loaded") is False:
                raise StaleBoardError(
                    f"{stale} The bridge could not reload the board from disk in "
                    "place (pcbnew board.Load() is unavailable in embedded "
                    "Python), so the mutation was refused to avoid overwriting "
                    "the newer on-disk file. Revert/reload the board in the "
                    "KiCad PCB editor (File > Revert), then retry.",
                    stale.disk_mtime, stale.loaded_mtime,
                ) from None
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

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        return self._call("remove_component", path, reference=reference)

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

    def set_footprint_value(
        self, path: Path, reference: str, value: str,
    ) -> dict[str, Any]:
        return self._call("set_footprint_value", path,
                          reference=reference, value=value)

    def refill_zones(self, path: Path) -> dict[str, Any]:
        return self._call("refill_zones", path)

    def save_board(self, path: Path) -> dict[str, Any]:
        return self._call("save_board", path)

    def clear_routes(self, path: Path, backup: bool = True) -> dict[str, Any]:
        return self._call("clear_routes", path, backup=backup)

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
        anchors: list[str] | None = None,
        strategy: str = "net_aware",
    ) -> dict[str, Any]:
        if strategy == "row":
            # Legacy geometry packer runs inside pcbnew (bridge), unchanged.
            return self._call("auto_place", path,
                              board_x=board_x, board_y=board_y,
                              board_width=board_width, board_height=board_height,
                              clearance_mm=clearance_mm,
                              anchors=anchors or [])

        # Net-aware: the engine is pure Python and lives here on the server side.
        # We refresh the on-disk board from the live session, compute the plan
        # from it, then apply each position through the *existing* bridge
        # move_component path (no new bridge handler, no reinstall). Anchored refs
        # are never moved (AC7).
        from kicad_mcp.backends.file_backend import build_engine_parts
        from kicad_mcp.utils import placement_engine as engine

        try:
            self._call("save_board", path)  # live board -> disk, so the plan is current
        except Exception:  # noqa: BLE001 — proceed with whatever is on disk
            pass

        parts = build_engine_parts(path, path.parent)
        if not parts:
            return {
                "components_placed": 0, "rows": 0, "total_area_mm2": 0.0,
                "placements": [], "warnings": [], "strategy": "net_aware",
            }

        items, warnings, total_area = engine.compute_net_aware_plan(
            parts, board_x, board_y, board_width, board_height,
            clearance_mm, anchors,
            diff_pair_nets=engine.read_diff_pair_nets(path),
        )
        placements: list[dict[str, Any]] = []
        applied_warnings: list[Any] = list(warnings)
        for ref, x, y, rot in items:
            try:
                self.move_component(path, ref, x, y, rotation=rot)
                placements.append({"reference": ref, "x": x, "y": y})
            except Exception as exc:  # noqa: BLE001
                applied_warnings.append(f"{ref}: move failed — {exc}")

        try:
            self._call("save_board", path)
        except Exception:  # noqa: BLE001
            pass

        return {
            "components_placed": len(placements),
            "rows": 0,
            "total_area_mm2": total_area,
            "placements": placements,
            "warnings": applied_warnings,
            "strategy": "net_aware",
        }

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
        """Try a ping; return True if a pcbnew bridge responds correctly."""
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            if not (isinstance(result, dict) and result.get("pong") is True):
                return False
            _validate_bridge_identity(result)
            return True
        except BridgeTemporarilyUnavailableError:
            # Wrong owner (e.g. project manager) or unreachable — not usable.
            return False
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
