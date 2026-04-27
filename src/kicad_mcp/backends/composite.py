"""Composite backend that routes operations to the best available backend."""

from __future__ import annotations

from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BackendProtocol,
    BoardOps,
    DRCOps,
    ExportOps,
    KiCadBackend,
    LibraryManageOps,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import CapabilityNotSupportedError

logger = get_logger("backend.composite")


class CompositeBackend(BackendProtocol):
    """Routes operations to the best available backend by capability.

    Priority order: IPC > SWIG > CLI > File
    Each operation type resolves to the highest-priority backend that supports it.
    """

    def __init__(self, backends: list[KiCadBackend], *, plugin_watchdog=None) -> None:
        self._backends = list(backends)
        self._plugin_watchdog = plugin_watchdog
        self._capability_map: dict[BackendCapability, KiCadBackend] = {}
        self._build_capability_map()

    def _build_capability_map(self) -> None:
        """Build the mapping from capabilities to their best backend."""
        # Priority order (last wins, so process in reverse priority)
        priority = {"file": 0, "pcbnew_subprocess": 1, "cli": 2, "swig": 3, "ipc": 4, "plugin": 5}
        sorted_backends = sorted(
            self._backends,
            key=lambda b: priority.get(b.name, -1),
        )

        for backend in sorted_backends:
            for cap in backend.capabilities:
                self._capability_map[cap] = backend
                logger.debug(
                    "Capability %s -> %s backend", cap.name, backend.name
                )

    def _get_backend_for(self, capability: BackendCapability) -> KiCadBackend:
        """Get the best backend for a given capability."""
        # Dynamic plugin discovery: if watchdog just became available, add it
        if (self._plugin_watchdog is not None
                and self._plugin_watchdog not in self._backends
                and self._plugin_watchdog.is_available()):
            self._backends.append(self._plugin_watchdog)
            self._build_capability_map()
            logger.info("Plugin backend came online; capability map rebuilt")

        backend = self._capability_map.get(capability)
        if backend is None:
            raise CapabilityNotSupportedError(
                f"No backend available for {capability.name}. "
                f"Available capabilities: {[c.name for c in self._capability_map]}"
            )
        return backend

    def get_board_ops(self) -> BoardOps:
        """Get board operations from the best available backend."""
        backend = self._get_backend_for(BackendCapability.BOARD_READ)
        ops = backend.get_board_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims BOARD_READ but returned no BoardOps"
            )
        return ops

    def export_dsn(self, path: "Path", dsn_path: "Path") -> dict:
        """Export DSN via best available BOARD_ROUTE backend.

        Routes to plugin bridge (reads live in-memory board) if plugin is active,
        otherwise falls back to subprocess pcbnew. Raises CapabilityNotSupportedError
        if no BOARD_ROUTE backend is available; raises RuntimeError on export failure.
        """
        backend = self._get_backend_for(BackendCapability.BOARD_ROUTE)
        ops = backend.get_board_ops()
        return ops.export_dsn(path, dsn_path)

    def import_ses(self, path: "Path", ses_path: "Path") -> dict:
        """Import SES via best available BOARD_ROUTE backend.

        Routes to plugin bridge (updates live in-memory board) if plugin active,
        otherwise falls back to subprocess pcbnew. Raises CapabilityNotSupportedError
        if no BOARD_ROUTE backend is available; raises RuntimeError on import failure.
        """
        backend = self._get_backend_for(BackendCapability.BOARD_ROUTE)
        ops = backend.get_board_ops()
        return ops.import_ses(path, ses_path)

    def reload_board(self, path: "Path") -> bool:
        """Reload pcbnew board from disk if the plugin backend is active.

        Call this after FreeRouting or other external processes write to the
        .kicad_pcb file, so subsequent plugin reads reflect the current file.
        Returns True if a reload was performed, False if plugin not active.
        """
        backend = self._capability_map.get(BackendCapability.BOARD_READ)
        if backend is not None and backend.name == "plugin":
            ops = backend.get_board_ops()
            if ops is not None and hasattr(ops, "reload_board"):
                try:
                    ops.reload_board(path)
                    return True
                except Exception as exc:
                    logger.debug("reload_board skipped: %s", exc)
        return False

    def save_board(self, path: "Path") -> bool:
        """Save pcbnew board to disk if the plugin backend is active.

        Call this before kicad-cli DRC/export so that any pcbnew in-memory
        changes made via the plugin are flushed to the .kicad_pcb file first.
        Returns True if a save was performed, False if plugin is not active or
        the path does not match the currently open board (not an error).
        """
        backend = self._capability_map.get(BackendCapability.BOARD_READ)
        if backend is not None and backend.name == "plugin":
            ops = backend.get_board_ops()
            if ops is not None and hasattr(ops, "save_board"):
                try:
                    ops.save_board(path)
                    return True
                except Exception as exc:
                    logger.debug("save_board skipped: %s", exc)
        return False

    def get_board_modify_ops(self) -> BoardOps:
        """Get board modification operations (requires BOARD_MODIFY capability)."""
        backend = self._get_backend_for(BackendCapability.BOARD_MODIFY)
        ops = backend.get_board_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims BOARD_MODIFY but returned no BoardOps"
            )
        return ops

    def get_schematic_ops(self) -> SchematicOps:
        """Get schematic operations from the best available backend."""
        backend = self._get_backend_for(BackendCapability.SCHEMATIC_READ)
        ops = backend.get_schematic_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims SCHEMATIC_READ but returned no SchematicOps"
            )
        return ops

    def get_schematic_modify_ops(self) -> SchematicOps:
        """Get schematic modification operations (requires SCHEMATIC_MODIFY capability)."""
        backend = self._get_backend_for(BackendCapability.SCHEMATIC_MODIFY)
        ops = backend.get_schematic_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims SCHEMATIC_MODIFY but returned no SchematicOps"
            )
        return ops

    def get_export_ops(self) -> ExportOps:
        """Get export operations from the best available backend."""
        backend = self._get_backend_for(BackendCapability.EXPORT_GERBER)
        ops = backend.get_export_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims EXPORT_GERBER but returned no ExportOps"
            )
        return ops

    def get_drc_ops(self) -> DRCOps:
        """Get DRC operations from the best available backend."""
        backend = self._capability_map.get(BackendCapability.DRC)
        if backend is None:
            # Fall back to ERC-only backend (e.g. file backend for run_erc)
            backend = self._capability_map.get(BackendCapability.ERC)
        if backend is None:
            raise CapabilityNotSupportedError(
                "No backend available for DRC or ERC. "
                f"Available capabilities: {[c.name for c in self._capability_map]}"
            )
        ops = backend.get_drc_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims DRC/ERC but returned no DRCOps"
            )
        return ops

    def get_library_ops(self) -> LibraryOps:
        """Get library operations from the best available backend."""
        backend = self._get_backend_for(BackendCapability.LIBRARY_SEARCH)
        ops = backend.get_library_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims LIBRARY_SEARCH but returned no LibraryOps"
            )
        return ops

    def get_library_manage_ops(self) -> LibraryManageOps:
        """Get library management (write) operations from the best available backend."""
        backend = self._get_backend_for(BackendCapability.LIBRARY_MANAGE)
        ops = backend.get_library_manage_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims LIBRARY_MANAGE but returned no LibraryManageOps"
            )
        return ops

    def get_zone_refill_ops(self) -> BoardOps | None:
        """Get board ops from the backend supporting ZONE_REFILL, or None."""
        backend = self._capability_map.get(BackendCapability.ZONE_REFILL)
        if backend is None:
            return None
        return backend.get_board_ops()

    def get_board_stackup_ops(self) -> BoardOps | None:
        """Get board ops from the backend supporting BOARD_STACKUP, or None."""
        backend = self._capability_map.get(BackendCapability.BOARD_STACKUP)
        if backend is None:
            return None
        return backend.get_board_ops()

    def get_text_variables(self, project_path: Any) -> dict[str, Any]:
        """Get project text variables (requires REAL_TIME_SYNC / IPC backend)."""
        backend = self._capability_map.get(BackendCapability.REAL_TIME_SYNC)
        if not backend:
            return {"status": "unavailable", "reason": "Requires KiCad running (IPC)"}
        return backend.get_text_variables(project_path)

    def set_text_variables(self, project_path: Any, variables: dict[str, str]) -> dict[str, Any]:
        """Set project text variables (requires REAL_TIME_SYNC / IPC backend)."""
        backend = self._capability_map.get(BackendCapability.REAL_TIME_SYNC)
        if not backend:
            return {"status": "unavailable", "reason": "Requires KiCad running (IPC)"}
        return backend.set_text_variables(project_path, variables)

    def get_active_project(self) -> dict[str, Any]:
        """Query the currently open KiCad project (requires REAL_TIME_SYNC)."""
        backend = self._get_backend_for(BackendCapability.REAL_TIME_SYNC)
        return backend.get_active_project()

    def has_capability(self, capability: BackendCapability) -> bool:
        """Check if any backend provides a given capability."""
        return capability in self._capability_map

    def get_status(self) -> dict[str, Any]:
        """Get status information about all backends."""
        backends_info = []
        for backend in self._backends:
            caps = [c.name for c in backend.capabilities]
            backends_info.append({
                "name": backend.name,
                "available": backend.is_available(),
                "version": backend.get_version(),
                "capabilities": caps,
            })

        primary = None
        for cap in [BackendCapability.BOARD_READ, BackendCapability.SCHEMATIC_READ]:
            if cap in self._capability_map:
                primary = self._capability_map[cap].name
                break

        return {
            "active_backends": backends_info,
            "primary_backend": primary or "none",
            "capability_routing": {
                cap.name: backend.name
                for cap, backend in self._capability_map.items()
            },
        }
