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
    LIBRARY_MANAGE = auto()
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
        x: float, y: float, rotation: float = 0.0,
        mirror: str | None = None, footprint: str = "",
        properties: dict[str, str] | None = None,
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

    def get_symbol_pin_positions(
        self, path: Path, reference: str,
    ) -> dict[str, Any]:
        """Get absolute schematic coordinates for each pin of a placed symbol."""
        raise NotImplementedError("This backend does not support pin position queries")

    def add_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        """Add a no-connect marker at the given position."""
        raise NotImplementedError("This backend does not support schematic modification")

    def add_power_symbol(
        self, path: Path, name: str, x: float, y: float, rotation: float = 0.0,
    ) -> dict[str, Any]:
        """Add a power symbol (e.g. +3V3, GND) at the given position."""
        raise NotImplementedError("This backend does not support schematic modification")

    def add_junction(self, path: Path, x: float, y: float) -> dict[str, Any]:
        """Add a junction dot at the given position."""
        raise NotImplementedError("This backend does not support schematic modification")

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        """Remove a placed component symbol from the schematic by reference designator."""
        raise NotImplementedError("This backend does not support schematic modification")

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        """Move a schematic symbol to a new position, optionally updating rotation."""
        raise NotImplementedError("This backend does not support schematic modification")

    def update_component_property(
        self, path: Path, reference: str,
        property_name: str, property_value: str,
    ) -> dict[str, Any]:
        """Update or add a property on a placed schematic symbol."""
        raise NotImplementedError("This backend does not support schematic modification")

    def remove_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        """Remove a wire segment identified by its start/end coordinates."""
        raise NotImplementedError("This backend does not support schematic modification")

    def remove_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        """Remove a no-connect marker identified by its position."""
        raise NotImplementedError("This backend does not support schematic modification")

    def get_sheet_hierarchy(self, path: Path) -> dict[str, Any]:
        """Get hierarchical sheet tree structure from a root schematic."""
        raise NotImplementedError("This backend does not support hierarchy queries")

    def validate_schematic(self, path: Path) -> dict[str, Any]:
        """Run file-based electrical rules validation (no kicad-cli needed)."""
        raise NotImplementedError("This backend does not support schematic validation")

    def get_net_connections(self, path: Path, net_name: str) -> dict[str, Any]:
        """Get all connections on a given net in the schematic."""
        raise NotImplementedError("This backend does not support net connectivity queries")

    def get_pin_net(self, path: Path, reference: str, pin_number: str) -> dict[str, Any]:
        """Get the net name connected to a specific pin of a symbol."""
        raise NotImplementedError("This backend does not support net connectivity queries")

    def create_schematic(
        self, path: Path, title: str = "", revision: str = "",
    ) -> dict[str, Any]:
        """Create a new, empty KiCad schematic file."""
        raise NotImplementedError("This backend does not support schematic creation")

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

    def suggest_footprints(self, lib_id: str) -> dict[str, Any]:
        """Suggest matching footprints for a symbol based on its fp_filter patterns."""
        raise NotImplementedError("This backend does not support footprint suggestions")


class LibraryManageOps(ABC):
    """Abstract interface for library management (write) operations."""

    @abstractmethod
    def clone_library_repo(
        self, url: str, name: str, target_path: str | None = None,
    ) -> dict[str, Any]:
        """Clone an external KiCad library repository."""

    @abstractmethod
    def register_library_source(self, path: str, name: str) -> dict[str, Any]:
        """Register a local directory as a searchable library source."""

    @abstractmethod
    def list_library_sources(self) -> list[dict[str, Any]]:
        """List all registered library sources."""

    @abstractmethod
    def unregister_library_source(self, name: str) -> dict[str, Any]:
        """Remove a library source registration (keeps files on disk)."""

    @abstractmethod
    def search_library_sources(
        self, query: str, source_name: str | None = None,
    ) -> dict[str, Any]:
        """Search for symbols and footprints across registered library sources."""

    @abstractmethod
    def create_project_library(
        self, project_path: str, library_name: str, lib_type: str = "both",
    ) -> dict[str, Any]:
        """Create an empty project-local library (.kicad_sym and/or .pretty)."""

    @abstractmethod
    def import_symbol(
        self, source_lib: str, symbol_name: str, target_lib_path: str,
    ) -> dict[str, Any]:
        """Copy a symbol definition from a source .kicad_sym to a target .kicad_sym."""

    @abstractmethod
    def import_footprint(
        self, source_lib: str, footprint_name: str, target_lib_path: str,
    ) -> dict[str, Any]:
        """Copy a .kicad_mod file from a source .pretty dir to a target .pretty dir."""

    @abstractmethod
    def register_project_library(
        self, project_path: str, library_name: str,
        library_path: str, lib_type: str,
    ) -> dict[str, Any]:
        """Add an entry to a project's sym-lib-table or fp-lib-table."""


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

    def get_library_manage_ops(self) -> LibraryManageOps | None:
        """Get library management (write) operations handler, or None if not supported."""
        return None

    def get_active_project(self) -> dict[str, Any]:
        """Query the currently open KiCad project via IPC.

        Returns:
            Dict with project_name, project_path, and open_documents list.
        """
        raise NotImplementedError("This backend does not support active project queries")
