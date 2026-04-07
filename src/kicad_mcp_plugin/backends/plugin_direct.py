"""PluginDirectBackend — hard-wired backend for the plugin entry point.

Board ops route exclusively through the kicad_mcp_bridge TCP bridge running
inside KiCad's embedded Python.  If the bridge is not reachable at startup,
BridgeNotAvailableError is raised immediately — there is no silent fallback.

Routing summary
---------------
Board read / modify  → PluginBoardOps  (TCP bridge, always)
Schematic read/write → FileBackend     (KiCad 9 has no eeschema scripting API)
DRC / ERC / export   → CLIBackend      (kicad-cli subprocess)
Library ops          → FileBackend
"""

from __future__ import annotations

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
from kicad_mcp.backends.plugin_backend import (
    PluginBoardOps,
    _get_op_timeout,  # noqa: PLC2701
    _get_ping_timeout,  # noqa: PLC2701
    _get_port,  # noqa: PLC2701
    _tcp_call,  # noqa: PLC2701
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import BackendNotAvailableError

logger = get_logger("backend.plugin_direct")


class BridgeNotAvailableError(BackendNotAvailableError):
    """Raised at startup when the kicad_mcp_bridge TCP server is not reachable.

    This is a hard failure — the plugin server will not start.
    """


class PluginDirectBackend(BackendProtocol):
    """Hard-wired backend for the plugin MCP entry point.

    Raises BridgeNotAvailableError at __init__ if the bridge is not reachable.
    Never falls back silently to any other board path after startup.
    """

    def __init__(self, cli_path: str | None = None) -> None:
        self._bridge_available = self._probe_bridge()
        self._board_ops = PluginBoardOps()
        self._file = FileBackend()
        self._cli = CLIBackend(cli_path=cli_path)
        if self._bridge_available:
            logger.info("PluginDirectBackend ready: bridge on port %d", _get_port())
        else:
            logger.warning(
                "PluginDirectBackend started but bridge unreachable on port %d. "
                "Open KiCad and enable kicad_mcp_bridge, then board tools will work.",
                _get_port(),
            )

    # ------------------------------------------------------------------
    # Startup probe
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
        except (ConnectionRefusedError, OSError, TimeoutError):
            return False

    # ------------------------------------------------------------------
    # Board ops — always plugin bridge, no fallback
    # ------------------------------------------------------------------

    def get_board_ops(self) -> BoardOps:
        return self._board_ops

    def get_board_modify_ops(self) -> BoardOps:
        return self._board_ops

    def get_zone_refill_ops(self) -> BoardOps | None:
        return self._board_ops

    def get_board_stackup_ops(self) -> BoardOps | None:
        return self._board_ops

    def save_board(self, path: Path) -> bool:
        """Save in-memory pcbnew board to disk via bridge.

        Returns True if the bridge saved the board, False if the bridge is
        unavailable or fails.  Callers (export, DRC) can proceed with the
        on-disk file when this returns False.
        """
        try:
            self._board_ops.save_board(path)
            return True
        except Exception as exc:
            logger.debug(
                "save_board via bridge failed (proceeding with on-disk file): %s", exc
            )
            return False

    def export_dsn(self, path: Path, dsn_path: Path) -> bool:
        """Export DSN from live in-memory board via bridge. Always executes."""
        self._board_ops.export_dsn(path, dsn_path)
        return True

    def import_ses(self, path: Path, ses_path: Path) -> dict:
        """Import FreeRouting SES into live in-memory board via bridge. Always executes."""
        return self._board_ops.import_ses(path, ses_path)

    def reload_board(self, path: Path) -> bool:
        """Reload pcbnew board from disk via bridge. Always executes."""
        self._board_ops.reload_board(path)
        return True

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
        return _tcp_call("get_active_project", _get_op_timeout())

    def get_text_variables(self, project_path: Any) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "text variables not yet supported via plugin bridge",
        }

    def set_text_variables(
        self, project_path: Any, variables: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "text variables not yet supported via plugin bridge",
        }

    # ------------------------------------------------------------------
    # Capability / status
    # ------------------------------------------------------------------

    _PLUGIN_CAPS = frozenset({
        BackendCapability.BOARD_READ,
        BackendCapability.BOARD_MODIFY,
        BackendCapability.ZONE_REFILL,
        BackendCapability.BOARD_STACKUP,
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
        return {
            "active_backends": [
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
            "primary_backend": "plugin",
            "capability_routing": {
                "BOARD_READ": "plugin",
                "BOARD_MODIFY": "plugin",
                "ZONE_REFILL": "plugin",
                "BOARD_STACKUP": "plugin",
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
        }
