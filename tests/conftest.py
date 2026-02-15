"""Shared test fixtures and mock backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    DRCOps,
    ExportOps,
    KiCadBackend,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.utils.change_log import ChangeLog

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_board_path() -> Path:
    return FIXTURES_DIR / "sample_board.kicad_pcb"


@pytest.fixture
def sample_schematic_path() -> Path:
    return FIXTURES_DIR / "sample_schematic.kicad_sch"


@pytest.fixture
def sample_project_path() -> Path:
    return FIXTURES_DIR / "sample_project.kicad_pro"


@pytest.fixture
def tmp_change_log(tmp_path: Path) -> ChangeLog:
    return ChangeLog(tmp_path / "test_changes.jsonl")


class MockBoardOps(BoardOps):
    """Mock board operations for testing."""

    def read_board(self, path: Path) -> dict[str, Any]:
        return {
            "info": self.get_board_info(path),
            "components": self.get_components(path),
            "nets": self.get_nets(path),
            "tracks": self.get_tracks(path),
        }

    def get_board_info(self, path: Path) -> dict[str, Any]:
        return {
            "file_path": str(path),
            "title": "Mock Board",
            "num_components": 3,
            "num_nets": 5,
            "num_tracks": 3,
        }

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return [
            {"reference": "R1", "value": "10k", "footprint": "R_0805"},
            {"reference": "R2", "value": "4.7k", "footprint": "R_0805"},
            {"reference": "U1", "value": "ATtiny85", "footprint": "SOIC-8"},
        ]

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return [
            {"number": 1, "name": "VCC"},
            {"number": 2, "name": "GND"},
        ]

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        return [
            {"start": {"x": 100, "y": 50}, "end": {"x": 110, "y": 50}, "width": 0.25},
        ]

    def place_component(self, path, reference, footprint, x, y, layer="F.Cu", rotation=0.0):
        return {"reference": reference, "footprint": footprint, "position": {"x": x, "y": y}}

    def move_component(self, path, reference, x, y, rotation=None):
        return {"reference": reference, "position": {"x": x, "y": y}}

    def add_track(self, path, start_x, start_y, end_x, end_y, width, layer="F.Cu", net=""):
        return {"start": {"x": start_x, "y": start_y}, "end": {"x": end_x, "y": end_y}}

    def add_via(self, path, x, y, size=0.8, drill=0.4, net="", via_type="through"):
        return {"position": {"x": x, "y": y}, "size": size}

    def assign_net(self, path, reference, pad, net):
        return {"reference": reference, "pad": pad, "net": net}

    def get_design_rules(self, path):
        return {"min_track_width": 0.2, "min_clearance": 0.15}


class MockSchematicOps(SchematicOps):
    """Mock schematic operations for testing."""

    def read_schematic(self, path: Path) -> dict[str, Any]:
        return {
            "info": {"file_path": str(path), "num_symbols": 3, "num_wires": 3, "num_labels": 3},
            "symbols": self.get_symbols(path),
            "wires": [],
            "labels": [],
        }

    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        return [
            {"reference": "R1", "value": "10k", "lib_id": "Device:R"},
            {"reference": "U1", "value": "ATtiny85", "lib_id": "MCU:ATtiny85"},
        ]

    def add_component(self, path, lib_id, reference, value, x, y):
        return {"reference": reference, "value": value, "lib_id": lib_id}

    def add_wire(self, path, start_x, start_y, end_x, end_y):
        return {"start": {"x": start_x, "y": start_y}, "end": {"x": end_x, "y": end_y}}

    def add_label(self, path, text, x, y, label_type="net_label"):
        return {"text": text, "position": {"x": x, "y": y}}


class MockExportOps(ExportOps):
    """Mock export operations for testing."""

    def export_gerbers(self, board_path, output_dir, layers=None):
        return {"success": True, "output_dir": str(output_dir), "output_files": []}

    def export_drill(self, board_path, output_dir):
        return {"success": True, "output_dir": str(output_dir), "output_files": []}

    def export_bom(self, path, output, fmt="csv"):
        return {"success": True, "output_files": [str(output)]}

    def export_pick_and_place(self, board_path, output):
        return {"success": True, "output_files": [str(output)]}

    def export_pdf(self, path, output, layers=None):
        return {"success": True, "output_files": [str(output)]}


class MockDRCOps(DRCOps):
    """Mock DRC operations for testing."""

    def run_drc(self, board_path, output=None):
        return {"passed": True, "error_count": 0, "warning_count": 0, "violations": []}

    def run_erc(self, schematic_path, output=None):
        return {"passed": True, "error_count": 0, "warning_count": 0, "violations": []}


class MockLibraryOps(LibraryOps):
    """Mock library operations for testing."""

    def search_symbols(self, query):
        return [{"name": "R", "library": "Device", "lib_id": "Device:R"}]

    def search_footprints(self, query):
        return [{"name": "R_0805", "library": "Resistor_SMD", "lib_id": "Resistor_SMD:R_0805"}]

    def list_libraries(self):
        return [
            {"name": "Device", "type": "symbol"},
            {"name": "Resistor_SMD", "type": "footprint"},
        ]

    def get_symbol_info(self, lib_id):
        return {"name": "R", "library": "Device", "description": "Resistor", "pin_count": 2}

    def get_footprint_info(self, lib_id):
        return {"name": "R_0805", "library": "Resistor_SMD", "pad_count": 2, "smd": True}


class MockBackend(KiCadBackend):
    """Full mock backend for testing."""

    def __init__(self, name_str: str = "mock", caps: set[BackendCapability] | None = None):
        self._name = name_str
        self._caps = caps or {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.SCHEMATIC_READ,
            BackendCapability.SCHEMATIC_MODIFY,
            BackendCapability.DRC,
            BackendCapability.ERC,
            BackendCapability.EXPORT_GERBER,
            BackendCapability.EXPORT_DRILL,
            BackendCapability.EXPORT_PDF,
            BackendCapability.EXPORT_BOM,
            BackendCapability.LIBRARY_SEARCH,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set[BackendCapability]:
        return self._caps

    def is_available(self) -> bool:
        return True

    def get_version(self) -> str:
        return "9.0.0-test"

    def get_board_ops(self) -> MockBoardOps:
        return MockBoardOps()

    def get_schematic_ops(self) -> MockSchematicOps:
        return MockSchematicOps()

    def get_export_ops(self) -> MockExportOps:
        return MockExportOps()

    def get_drc_ops(self) -> MockDRCOps:
        return MockDRCOps()

    def get_library_ops(self) -> MockLibraryOps:
        return MockLibraryOps()


@pytest.fixture
def mock_backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def mock_composite(mock_backend: MockBackend) -> CompositeBackend:
    return CompositeBackend([mock_backend])
