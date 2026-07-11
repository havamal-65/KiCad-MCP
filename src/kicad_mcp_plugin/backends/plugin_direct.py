"""PluginDirectBackend — hard-wired backend for the plugin entry point.

Live-board ops route **IPC-first** (kicad-python/kipy against the KiCad IPC API)
with the SWIG plugin bridge as fallback, then — only when no live path is up —
the file backend, gated by a safe-refuse rule for writes (F1, bridge-board-access
spec §4). The bridge is kept, not retired (NG3).

Routing summary
---------------
Board read           → IPC → bridge → file (stale-flagged if KiCad is open)
Board modify         → IPC → bridge → file *only if KiCad closed* → SafeRefuseError
Schematic read/write → FileBackend     (KiCad 9 has no eeschema scripting API)
DRC / ERC / export   → CLIBackend      (kicad-cli subprocess)
Library ops          → FileBackend
Routing (DSN/SES)    → bridge/subprocess (unchanged — BOARD_ROUTE)

Per-op fallback: ops the IPC backend cannot perform (in-commit validation
shortfalls like place-by-lib-id, the legacy "row" auto_place packer, bridge
extras) raise NotImplementedError and are transparently retried on the bridge
(REQ-ROUTE-4).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BackendProtocol,
    BoardOps,
    DRCOps,
    ExportOps,
    LibraryManageOps,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.backends.cli_backend import CLIBackend
from kicad_mcp.backends.file_backend import FileBackend
from kicad_mcp.backends.ipc_backend import IPCBackend
from kicad_mcp.backends.ipc_connection import IPCUnavailableError, connection_remedy
from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
    _get_op_timeout,  # noqa: PLC2701
    _get_ping_timeout,  # noqa: PLC2701
    _get_port,  # noqa: PLC2701
    _tcp_call,  # noqa: PLC2701
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import BackendNotAvailableError, SafeRefuseError
from kicad_mcp.utils.platform_helper import is_kicad_running

logger = get_logger("backend.plugin_direct")


class BridgeNotAvailableError(BackendNotAvailableError):
    """Raised at startup when the kicad_mcp_bridge TCP server is not reachable.

    This is a hard failure — the plugin server will not start.
    """


class _LiveBoardOps(BoardOps):
    """IPC-primary board ops with transparent per-op bridge fallback.

    Every op is served from the IPC backend first. Ops IPC cannot perform —
    the ``BoardOps`` base default for S2 rows, an in-commit validation
    shortfall (place-by-lib-id, net creation), a bridge-only extra method, or
    an IPC connection drop mid-op — are retried verbatim on the SWIG bridge
    (REQ-ROUTE-2/4). The path that actually served each op is recorded for
    ``get_status`` telemetry (REQ-ROUTE-5).
    """

    def __init__(
        self,
        ipc_ops: BoardOps,
        bridge_supplier: Callable[[], BoardOps],
        record_path: Callable[[str], None],
    ) -> None:
        self._ipc_ops = ipc_ops
        self._bridge_supplier = bridge_supplier
        self._record_path = record_path

    def _call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        try:
            result = getattr(self._ipc_ops, method)(*args, **kwargs)
        except (NotImplementedError, AttributeError, IPCUnavailableError) as exc:
            bridge = self._bridge_supplier()  # raises if the bridge is down too
            logger.debug("op %s fell through IPC → bridge (%s)", method, exc)
            self._record_path("bridge")
            return getattr(bridge, method)(*args, **kwargs)
        self._record_path("ipc")
        return result

    # -- reads --
    def read_board(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("read_board", path)
        return result

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("get_components", path)
        return result

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("get_nets", path)
        return result

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("get_tracks", path)
        return result

    def get_board_info(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("get_board_info", path)
        return result

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("get_design_rules", path)
        return result

    def get_stackup(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("get_stackup", path)
        return result

    def get_board_info_extended(self, path: Path) -> dict[str, Any]:
        # bridge extra (not on the BoardOps base) — _call falls through on
        # the IPC side's AttributeError
        result: dict[str, Any] = self._call("get_board_info_extended", path)
        return result

    # -- writes --
    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "place_component", path, reference, footprint, x, y, layer, rotation)
        return result

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "move_component", path, reference, x, y, rotation)
        return result

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("remove_component", path, reference)
        return result

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "add_track", path, start_x, start_y, end_x, end_y, width, layer, net)
        return result

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "add_via", path, x, y, size, drill, net, via_type)
        return result

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call("assign_net", path, reference, pad, net)
        return result

    def set_footprint_value(
        self, path: Path, reference: str, value: str,
    ) -> dict[str, Any]:
        # bridge extra (not on the BoardOps base)
        result: dict[str, Any] = self._call(
            "set_footprint_value", path, reference, value)
        return result

    def refill_zones(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("refill_zones", path)
        return result

    def save_board(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("save_board", path)
        return result

    def reload_board(self, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("reload_board", path)
        return result

    def add_board_outline(
        self, path: Path, x: float, y: float,
        width: float, height: float, line_width: float = 0.05,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "add_board_outline", path, x, y, width, height, line_width)
        return result

    def auto_place(
        self, path: Path, board_x: float, board_y: float,
        board_width: float, board_height: float, clearance_mm: float = 1.5,
        anchors: list[str] | None = None,
        strategy: str = "net_aware",
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "auto_place", path, board_x, board_y, board_width, board_height,
            clearance_mm, anchors, strategy)
        return result

    def place_components_bulk(
        self, path: Path, components: list[dict],  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "place_components_bulk", path, components)
        return result

    def export_dsn(self, path: Path, dsn_path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("export_dsn", path, dsn_path)
        return result

    def import_ses(self, path: Path, ses_path: Path) -> dict[str, Any]:
        result: dict[str, Any] = self._call("import_ses", path, ses_path)
        return result

    def clear_routes(self, path: Path, backup: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = self._call("clear_routes", path, backup)
        return result

    def clean_board_for_routing(
        self, path: Path,
        remove_keepouts: bool = True,
        remove_unassigned_tracks: bool = True,
    ) -> dict[str, Any]:
        # IPC-only op (no bridge handler): if IPC falls through, the bridge
        # retry raises AttributeError, which the tool layer catches into its
        # headless disk path.
        result: dict[str, Any] = self._call(
            "clean_board_for_routing", path, remove_keepouts,
            remove_unassigned_tracks)
        return result


class _StaleFileBoardOps(BoardOps):
    """File-backend reads with an explicit staleness flag (REQ-SAFE-3).

    Used only when both live paths are down while KiCad is open: the .kicad_pcb
    on disk is the last *saved* state and the live board may hold newer unsaved
    edits. Dict-shaped read results carry ``stale: True``; list-shaped results
    (components/nets/tracks) pass through unchanged — their tool wrappers
    compose them under ``read_board``. Writes never route here (the resolver
    safe-refuses first) and keep the base NotImplementedError default.
    """

    def __init__(self, file_ops: BoardOps) -> None:
        self._file_ops = file_ops

    def read_board(self, path: Path) -> dict[str, Any]:
        result = self._file_ops.read_board(path)
        result["stale"] = True
        return result

    def get_board_info(self, path: Path) -> dict[str, Any]:
        result = self._file_ops.get_board_info(path)
        result["stale"] = True
        return result

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        result = self._file_ops.get_design_rules(path)
        result["stale"] = True
        return result

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return self._file_ops.get_components(path)

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return self._file_ops.get_nets(path)

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        return self._file_ops.get_tracks(path)


class PluginDirectBackend(BackendProtocol):
    """Hard-wired backend for the plugin MCP entry point.

    Raises BridgeNotAvailableError at __init__ if the bridge is not reachable.
    Never falls back silently to any other board path after startup.
    """

    def __init__(self, cli_path: Path | None = None) -> None:
        self._bridge_available = self._probe_bridge()
        self._board_ops = PluginBoardOps()
        self._board_ops._on_disconnect = self._on_bridge_disconnect
        self._file = FileBackend()
        self._cli = CLIBackend(cli_path=cli_path)
        # IPC is the primary live path (F1); construction never raises and
        # never probes — availability is resolved per-op (REQ-IPC-7).
        self._ipc = IPCBackend()
        self._live_path: dict[str, str] = {}
        if self._bridge_available:
            logger.info(
                "PluginDirectBackend ready: IPC-first live routing; "
                "bridge fallback on port %d", _get_port())
        else:
            logger.warning(
                "PluginDirectBackend started with bridge unreachable on port %d. "
                "Live board ops will use the IPC API when available; otherwise "
                "open KiCad and enable kicad_mcp_bridge.",
                _get_port(),
            )

    # ------------------------------------------------------------------
    # Startup probe + watchdog
    # ------------------------------------------------------------------

    def _probe_bridge(self) -> bool:
        """TCP-ping the bridge. Returns True if reachable, False otherwise."""
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            if not isinstance(result, dict) or result.get("pong") is not True:
                logger.warning(
                    "Bridge responded but pong check failed on port %d. "
                    "Is the correct version of kicad_mcp_bridge installed?",
                    _get_port(),
                )
                return False
            return True
        except (ConnectionRefusedError, OSError, TimeoutError,
                BridgeTemporarilyUnavailableError):
            return False

    def _on_bridge_disconnect(self) -> None:
        """Called when PluginBoardOps detects a mid-session connection failure."""
        if self._bridge_available:
            logger.warning(
                "Bridge connection lost on port %d. "
                "Marking bridge unavailable — reopen KiCad to restore board operations.",
                _get_port(),
            )
        self._bridge_available = False

    def _check_bridge(self) -> None:
        """Re-probe the bridge if it was previously marked unavailable.

        Raises BridgeTemporarilyUnavailableError with a helpful message if the
        bridge is still down after re-probing.
        """
        if self._bridge_available:
            return
        # Bridge was marked down — try once more before failing
        if self._probe_bridge():
            self._bridge_available = True
            logger.info("Bridge reconnected on port %d.", _get_port())
            return
        raise BridgeTemporarilyUnavailableError(
            f"Bridge unreachable on port {_get_port()}. "
            "Open KiCad and ensure kicad_mcp_bridge is active, then retry."
        )

    # ------------------------------------------------------------------
    # Board ops — IPC → bridge → file/safe-refuse (spec §4.1)
    # ------------------------------------------------------------------

    def _bridge_reachable(self) -> bool:
        """Non-raising reachability check with one re-probe after a drop."""
        if self._bridge_available:
            return True
        if self._probe_bridge():
            self._bridge_available = True
            logger.info("Bridge reconnected on port %d.", _get_port())
            return True
        return False

    def _bridge_ops(self) -> BoardOps:
        """Bridge ops for the per-op fallback; raises if the bridge is down."""
        self._check_bridge()
        return self._board_ops

    def _file_board_ops(self) -> BoardOps:
        ops = self._file.get_board_ops()
        assert ops is not None
        return ops

    def _record_live_path(self, cap: BackendCapability, path: str) -> None:
        self._live_path[cap.name] = path

    def _resolve_live_ops(self, cap: BackendCapability, *, write: bool) -> BoardOps:
        """REQ-ROUTE-1/2/3 + REQ-SAFE-1/2/3.

        IPC first (server reachable AND a loaded board open), then the SWIG
        bridge. With both live paths down: writes go to the file backend only
        when KiCad is closed (no live session to clobber) and otherwise
        safe-refuse; reads degrade to the file backend, stale-flagged when a
        live KiCad may hold newer unsaved state.
        """
        if self._ipc.is_available():
            self._live_path[cap.name] = "ipc"
            return _LiveBoardOps(
                self._ipc.get_board_ops(),
                self._bridge_ops,
                lambda path: self._record_live_path(cap, path),
            )
        if self._bridge_reachable():
            self._live_path[cap.name] = "bridge"
            return self._board_ops
        kicad_open = is_kicad_running()
        if write:
            if kicad_open:
                # REQ-SAFE-1 — never disk-write over a live open board (#14C)
                remedy = connection_remedy()
                raise SafeRefuseError(
                    f"Board write refused: KiCad is open but neither the IPC "
                    f"API nor the kicad_mcp_bridge is reachable, and writing "
                    f"the file on disk would be clobbered by KiCad's in-memory "
                    f"state. {remedy}",
                    capability=cap.name,
                    remedy=remedy,
                    paths_tried=["ipc", "bridge"],
                )
            self._live_path[cap.name] = "file"  # REQ-SAFE-2: KiCad closed
            return self._file_board_ops()
        if kicad_open:
            # REQ-SAFE-3 — saved state may lag the live board: flag it
            self._live_path[cap.name] = "file:stale"
            return _StaleFileBoardOps(self._file_board_ops())
        self._live_path[cap.name] = "file"  # KiCad closed: disk is the truth
        return self._file_board_ops()

    def get_board_ops(self) -> BoardOps:
        return self._resolve_live_ops(BackendCapability.BOARD_READ, write=False)

    def get_board_modify_ops(self) -> BoardOps:
        return self._resolve_live_ops(BackendCapability.BOARD_MODIFY, write=True)

    def get_zone_refill_ops(self) -> BoardOps | None:
        return self._resolve_live_ops(BackendCapability.ZONE_REFILL, write=True)

    def get_board_stackup_ops(self) -> BoardOps | None:
        return self._resolve_live_ops(BackendCapability.BOARD_STACKUP, write=False)

    def save_board(self, path: Path) -> bool:
        """Save in-memory pcbnew board to disk via bridge.

        Returns True if the bridge saved the board, False if the bridge is
        unavailable or fails.  Callers (export, DRC) can proceed with the
        on-disk file when this returns False.
        """
        try:
            self._board_ops.save_board(path)
            return True
        except BridgeTemporarilyUnavailableError:
            self._on_bridge_disconnect()
            logger.debug("save_board skipped: bridge unavailable")
            return False
        except Exception as exc:
            logger.debug(
                "save_board via bridge failed (proceeding with on-disk file): %s", exc
            )
            return False

    def export_dsn(self, path: Path, dsn_path: Path) -> dict[str, Any]:
        """Export DSN from live in-memory board via bridge. Always executes."""
        try:
            return self._board_ops.export_dsn(path, dsn_path)
        except BridgeTemporarilyUnavailableError:
            self._on_bridge_disconnect()
            raise

    def import_ses(self, path: Path, ses_path: Path) -> dict[str, Any]:
        """Import FreeRouting SES into live in-memory board via bridge. Always executes."""
        try:
            return self._board_ops.import_ses(path, ses_path)
        except BridgeTemporarilyUnavailableError:
            self._on_bridge_disconnect()
            raise

    def reload_board(self, path: Path) -> bool:
        """Reload pcbnew board from disk via bridge. Always executes."""
        try:
            self._board_ops.reload_board(path)
            return True
        except BridgeTemporarilyUnavailableError:
            self._on_bridge_disconnect()
            raise

    # ------------------------------------------------------------------
    # Schematic ops — always file backend (KiCad 9 platform constraint)
    # ------------------------------------------------------------------

    def get_schematic_ops(self) -> SchematicOps:
        ops = self._file.get_schematic_ops()
        assert ops is not None
        return ops

    def get_schematic_modify_ops(self) -> SchematicOps:
        # No _check_file_write_safety needed: pcbnew does not hold .kicad_sch
        # files open in memory, so file writes are always safe.
        ops = self._file.get_schematic_ops()
        assert ops is not None
        return ops

    # ------------------------------------------------------------------
    # DRC / export — kicad-cli
    # ------------------------------------------------------------------

    def get_export_ops(self) -> ExportOps:
        ops = self._cli.get_export_ops()
        if ops is None:
            from kicad_mcp.models.errors import CapabilityNotSupportedError
            raise CapabilityNotSupportedError(
                "kicad-cli not available. Install KiCad and ensure kicad-cli is on PATH."
            )
        return ops

    def get_drc_ops(self) -> DRCOps:
        ops = self._cli.get_drc_ops()
        if ops is None:
            from kicad_mcp.models.errors import CapabilityNotSupportedError
            raise CapabilityNotSupportedError(
                "kicad-cli not available. Install KiCad and ensure kicad-cli is on PATH."
            )
        return ops

    # ------------------------------------------------------------------
    # Library ops — file backend
    # ------------------------------------------------------------------

    def get_library_ops(self) -> LibraryOps:
        ops = self._file.get_library_ops()
        assert ops is not None
        return ops

    def get_library_manage_ops(self) -> LibraryManageOps:
        ops = self._file.get_library_manage_ops()
        assert ops is not None
        return ops

    # ------------------------------------------------------------------
    # Project / IPC ops
    # ------------------------------------------------------------------

    def get_active_project(self) -> dict[str, Any]:
        # IPC first — the board path is the exact datum the bridge loses after
        # an in-place mutation (#14 GetFileName detachment).
        if self._ipc.is_available():
            try:
                result = self._ipc.get_active_project()
                self._live_path["ACTIVE_PROJECT"] = "ipc"
                return result
            except IPCUnavailableError:
                pass  # dropped between probe and call — fall to the bridge
        try:
            bridge_result: dict[str, Any] = _tcp_call(
                "get_active_project", _get_op_timeout())
            self._live_path["ACTIVE_PROJECT"] = "bridge"
            return bridge_result
        except BridgeTemporarilyUnavailableError:
            self._on_bridge_disconnect()
            raise

    def get_text_variables(self, project_path: Any) -> dict[str, Any]:
        # IPC first (spec §3 row 20): the live project may hold definitions
        # newer than the on-disk .kicad_pro (KiCad writes it on save/close).
        if self._ipc.is_available():
            try:
                result = self._ipc.get_text_variables(project_path)
                self._live_path["TEXT_VARS"] = "ipc"
                return result
            except (NotImplementedError, IPCUnavailableError):
                pass  # wrong project open, or dropped mid-op — use the file
        self._live_path["TEXT_VARS"] = "file"
        try:
            pro = json.loads(Path(project_path).read_text(encoding="utf-8"))
            return {"status": "success", "variables": pro.get("text_variables", {})}
        except FileNotFoundError:
            return {"status": "error", "message": f"Project file not found: {project_path}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def set_text_variables(
        self, project_path: Any, variables: dict[str, str]
    ) -> dict[str, Any]:
        # IPC first (spec §3 row 21): writing the .kicad_pro file-side while
        # KiCad is open gets clobbered when KiCad saves its in-memory project
        # state — the same hazard class as set_board_design_rules (d018367).
        if self._ipc.is_available():
            try:
                result = self._ipc.set_text_variables(project_path, variables)
                self._live_path["TEXT_VARS"] = "ipc"
                return result
            except (NotImplementedError, IPCUnavailableError):
                pass
        self._live_path["TEXT_VARS"] = "file"
        try:
            p = Path(project_path)
            pro = json.loads(p.read_text(encoding="utf-8"))
            pro["text_variables"] = variables
            p.write_text(json.dumps(pro, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return {"status": "success", "variables": variables, "count": len(variables)}
        except FileNotFoundError:
            return {"status": "error", "message": f"Project file not found: {project_path}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # Capability / status
    # ------------------------------------------------------------------

    _IPC_CAPS = frozenset({
        BackendCapability.BOARD_READ,
        BackendCapability.BOARD_MODIFY,
        BackendCapability.ZONE_REFILL,
        BackendCapability.BOARD_STACKUP,
    })
    _PLUGIN_CAPS = frozenset({
        BackendCapability.BOARD_READ,
        BackendCapability.BOARD_MODIFY,
        BackendCapability.ZONE_REFILL,
        BackendCapability.BOARD_STACKUP,
        BackendCapability.BOARD_ROUTE,
    })
    _FILE_CAPS = frozenset({
        BackendCapability.SCHEMATIC_READ,
        BackendCapability.SCHEMATIC_MODIFY,
        BackendCapability.LIBRARY_SEARCH,
        BackendCapability.LIBRARY_MANAGE,
        BackendCapability.ERC,
    })
    _CLI_CAPS = frozenset({
        BackendCapability.DRC,
        BackendCapability.ERC,
        BackendCapability.EXPORT_GERBER,
        BackendCapability.EXPORT_DRILL,
        BackendCapability.EXPORT_PDF,
        BackendCapability.EXPORT_BOM,
        BackendCapability.EXPORT_PICK_AND_PLACE,
        BackendCapability.NETLIST_GENERATE,
    })

    def has_capability(self, capability: BackendCapability) -> bool:
        return capability in (self._PLUGIN_CAPS | self._FILE_CAPS | self._CLI_CAPS)

    def get_status(self) -> dict[str, Any]:
        cli_available = self._cli.is_available()
        ipc_available = self._ipc.is_available()
        return {
            "active_backends": [
                {
                    "name": "ipc",
                    "available": ipc_available,
                    "capabilities": [c.name for c in self._IPC_CAPS],
                },
                {
                    "name": "plugin",
                    "available": True,
                    "capabilities": [c.name for c in self._PLUGIN_CAPS],
                },
                {
                    "name": "file",
                    "available": True,
                    "capabilities": [c.name for c in self._FILE_CAPS],
                },
                {
                    "name": "cli",
                    "available": cli_available,
                    "capabilities": [c.name for c in self._CLI_CAPS],
                },
            ],
            "primary_backend": "ipc" if ipc_available else "plugin",
            "capability_routing": {
                # Values stay plain strings: the health monitor groups by them.
                "BOARD_READ": "ipc→plugin→file",
                "BOARD_MODIFY": "ipc→plugin→file",
                "ZONE_REFILL": "ipc→plugin→file",
                "BOARD_STACKUP": "ipc→plugin→file",
                "SCHEMATIC_READ": "file",
                "SCHEMATIC_MODIFY": "file",
                "LIBRARY_SEARCH": "file",
                "LIBRARY_MANAGE": "file",
                "DRC": "cli",
                "ERC": "cli",
                "EXPORT_GERBER": "cli",
                "EXPORT_DRILL": "cli",
                "EXPORT_PDF": "cli",
                "EXPORT_BOM": "cli",
                "EXPORT_PICK_AND_PLACE": "cli",
                "NETLIST_GENERATE": "cli",
            },
            # REQ-ROUTE-5 — which path actually served the last board op, per
            # capability ("ipc" / "bridge" / "file" / "file:stale"). Sibling of
            # capability_routing so its string-valued contract stays intact.
            "live_path_last": dict(self._live_path),
        }
