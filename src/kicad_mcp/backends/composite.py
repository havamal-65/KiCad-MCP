"""Composite backend that routes operations to the best available backend."""

from __future__ import annotations

from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    DRCOps,
    ExportOps,
    KiCadBackend,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import CapabilityNotSupportedError

logger = get_logger("backend.composite")


class CompositeBackend:
    """Routes operations to the best available backend by capability.

    Priority order: IPC > SWIG > CLI > File
    Each operation type resolves to the highest-priority backend that supports it.
    """

    def __init__(self, backends: list[KiCadBackend]) -> None:
        self._backends = backends
        self._capability_map: dict[BackendCapability, KiCadBackend] = {}
        self._build_capability_map()

    def _build_capability_map(self) -> None:
        """Build the mapping from capabilities to their best backend."""
        # Priority order (last wins, so process in reverse priority)
        priority = {"file": 0, "cli": 1, "swig": 2, "ipc": 3}
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
        backend = self._get_backend_for(BackendCapability.DRC)
        ops = backend.get_drc_ops()
        if ops is None:
            raise CapabilityNotSupportedError(
                f"Backend '{backend.name}' claims DRC but returned no DRCOps"
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
