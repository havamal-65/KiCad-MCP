"""Abstract interfaces for KiCad backend operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum, auto
from pathlib import Path
from typing import Any


class BackendCapability(Enum):
    """Capabilities that backends can provide."""
    BOARD_READ = auto()
    BOARD_MODIFY = auto()
    SCHEMATIC_READ = auto()
    SCHEMATIC_MODIFY = auto()
    DRC = auto()
    ERC = auto()
    EXPORT_GERBER = auto()
    EXPORT_DRILL = auto()
    EXPORT_PDF = auto()
    EXPORT_BOM = auto()
    EXPORT_PICK_AND_PLACE = auto()
    LIBRARY_SEARCH = auto()
    NETLIST_GENERATE = auto()
    REAL_TIME_SYNC = auto()


class BoardOps(ABC):
    """Abstract interface for PCB board operations."""

    @abstractmethod
    def read_board(self, path: Path) -> dict[str, Any]:
        """Read board data and return structured info."""

    @abstractmethod
    def get_components(self, path: Path) -> list[dict[str, Any]]:
        """Get all components/footprints on the board."""

    @abstractmethod
    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        """Get all nets on the board."""

    @abstractmethod
    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        """Get all tracks on the board."""

    @abstractmethod
    def get_board_info(self, path: Path) -> dict[str, Any]:
        """Get board metadata (title, revision, layers, etc.)."""

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        """Place a component on the board."""
        raise NotImplementedError("This backend does not support board modification")

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        """Move a component to a new position."""
        raise NotImplementedError("This backend does not support board modification")

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        """Add a track segment."""
        raise NotImplementedError("This backend does not support board modification")

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        """Add a via."""
        raise NotImplementedError("This backend does not support board modification")

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        """Assign a net to a component pad."""
        raise NotImplementedError("This backend does not support board modification")

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        """Get the board's design rules."""
        raise NotImplementedError("This backend does not support design rule reading")


class SchematicOps(ABC):
    """Abstract interface for schematic operations."""

    @abstractmethod
    def read_schematic(self, path: Path) -> dict[str, Any]:
        """Read schematic data and return structured info."""

    @abstractmethod
    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        """Get all symbols in the schematic."""

    def add_component(
        self, path: Path, lib_id: str, reference: str, value: str,
        x: float, y: float,
    ) -> dict[str, Any]:
        """Add a component symbol to the schematic."""
        raise NotImplementedError("This backend does not support schematic modification")

    def add_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        """Add a wire connection."""
        raise NotImplementedError("This backend does not support schematic modification")

    def add_label(
        self, path: Path, text: str, x: float, y: float,
        label_type: str = "net_label",
    ) -> dict[str, Any]:
        """Add a net label."""
        raise NotImplementedError("This backend does not support schematic modification")

    def annotate(self, path: Path) -> dict[str, Any]:
        """Auto-annotate component references."""
        raise NotImplementedError("This backend does not support annotation")

    def generate_netlist(self, path: Path, output: Path) -> dict[str, Any]:
        """Generate a netlist from the schematic."""
        raise NotImplementedError("This backend does not support netlist generation")


class ExportOps(ABC):
    """Abstract interface for export operations."""

    def export_gerbers(
        self, board_path: Path, output_dir: Path, layers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export Gerber files."""
        raise NotImplementedError("This backend does not support Gerber export")

    def export_drill(
        self, board_path: Path, output_dir: Path,
    ) -> dict[str, Any]:
        """Export drill files."""
        raise NotImplementedError("This backend does not support drill export")

    def export_bom(
        self, path: Path, output: Path, fmt: str = "csv",
    ) -> dict[str, Any]:
        """Export bill of materials."""
        raise NotImplementedError("This backend does not support BOM export")

    def export_pick_and_place(
        self, board_path: Path, output: Path,
    ) -> dict[str, Any]:
        """Export pick-and-place file."""
        raise NotImplementedError("This backend does not support pick-and-place export")

    def export_pdf(
        self, path: Path, output: Path, layers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export PDF."""
        raise NotImplementedError("This backend does not support PDF export")


class DRCOps(ABC):
    """Abstract interface for design rule checks."""

    def run_drc(self, board_path: Path, output: Path | None = None) -> dict[str, Any]:
        """Run Design Rule Check on a board."""
        raise NotImplementedError("This backend does not support DRC")

    def run_erc(self, schematic_path: Path, output: Path | None = None) -> dict[str, Any]:
        """Run Electrical Rules Check on a schematic."""
        raise NotImplementedError("This backend does not support ERC")


class LibraryOps(ABC):
    """Abstract interface for library operations."""

    @abstractmethod
    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        """Search for symbols matching a query."""

    @abstractmethod
    def search_footprints(self, query: str) -> list[dict[str, Any]]:
        """Search for footprints matching a query."""

    def list_libraries(self) -> list[dict[str, Any]]:
        """List available libraries."""
        raise NotImplementedError("This backend does not support library listing")

    def get_symbol_info(self, lib_id: str) -> dict[str, Any]:
        """Get detailed info about a symbol."""
        raise NotImplementedError("This backend does not support symbol info")

    def get_footprint_info(self, lib_id: str) -> dict[str, Any]:
        """Get detailed info about a footprint."""
        raise NotImplementedError("This backend does not support footprint info")


class KiCadBackend(ABC):
    """Base class for all KiCad backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier."""

    @property
    @abstractmethod
    def capabilities(self) -> set[BackendCapability]:
        """Set of operations this backend supports."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend can be used in the current environment."""

    def get_version(self) -> str | None:
        """Get the KiCad version this backend connects to."""
        return None

    def get_board_ops(self) -> BoardOps | None:
        """Get board operations handler, or None if not supported."""
        return None

    def get_schematic_ops(self) -> SchematicOps | None:
        """Get schematic operations handler, or None if not supported."""
        return None

    def get_export_ops(self) -> ExportOps | None:
        """Get export operations handler, or None if not supported."""
        return None

    def get_drc_ops(self) -> DRCOps | None:
        """Get DRC operations handler, or None if not supported."""
        return None

    def get_library_ops(self) -> LibraryOps | None:
        """Get library operations handler, or None if not supported."""
        return None
