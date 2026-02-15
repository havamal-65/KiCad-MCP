"""Tests for the file-parsing backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBackend, FileBoardOps, FileSchematicOps


@pytest.fixture
def file_backend() -> FileBackend:
    return FileBackend()


@pytest.fixture
def board_ops() -> FileBoardOps:
    return FileBoardOps()


@pytest.fixture
def schematic_ops() -> FileSchematicOps:
    return FileSchematicOps()


class TestFileBackend:
    def test_always_available(self, file_backend: FileBackend):
        assert file_backend.is_available()

    def test_name(self, file_backend: FileBackend):
        assert file_backend.name == "file"

    def test_has_board_read(self, file_backend: FileBackend):
        from kicad_mcp.backends.base import BackendCapability
        assert BackendCapability.BOARD_READ in file_backend.capabilities

    def test_has_schematic_read(self, file_backend: FileBackend):
        from kicad_mcp.backends.base import BackendCapability
        assert BackendCapability.SCHEMATIC_READ in file_backend.capabilities

    def test_get_board_ops(self, file_backend: FileBackend):
        ops = file_backend.get_board_ops()
        assert ops is not None
        assert isinstance(ops, FileBoardOps)

    def test_get_schematic_ops(self, file_backend: FileBackend):
        ops = file_backend.get_schematic_ops()
        assert ops is not None
        assert isinstance(ops, FileSchematicOps)


class TestFileBoardOps:
    def test_read_board(self, board_ops: FileBoardOps, sample_board_path: Path):
        result = board_ops.read_board(sample_board_path)
        assert "info" in result
        assert "components" in result
        assert "nets" in result
        assert "tracks" in result

    def test_get_components(self, board_ops: FileBoardOps, sample_board_path: Path):
        components = board_ops.get_components(sample_board_path)
        assert len(components) == 3
        refs = {c.get("reference") for c in components}
        assert "R1" in refs
        assert "R2" in refs
        assert "U1" in refs

    def test_get_nets(self, board_ops: FileBoardOps, sample_board_path: Path):
        nets = board_ops.get_nets(sample_board_path)
        assert len(nets) == 5
        net_names = {n["name"] for n in nets}
        assert "VCC" in net_names
        assert "GND" in net_names

    def test_get_tracks(self, board_ops: FileBoardOps, sample_board_path: Path):
        tracks = board_ops.get_tracks(sample_board_path)
        assert len(tracks) == 3
        assert tracks[0]["width"] == 0.25

    def test_get_board_info(self, board_ops: FileBoardOps, sample_board_path: Path):
        info = board_ops.get_board_info(sample_board_path)
        assert info["title"] == "Sample Board"
        assert info["revision"] == "1.0"
        assert "num_components" in info
        assert "num_nets" in info

    def test_get_design_rules(self, board_ops: FileBoardOps, sample_board_path: Path):
        rules = board_ops.get_design_rules(sample_board_path)
        assert isinstance(rules, dict)


class TestFileSchematicOps:
    def test_read_schematic(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        result = schematic_ops.read_schematic(sample_schematic_path)
        assert "info" in result
        assert "symbols" in result
        assert "wires" in result
        assert "labels" in result

    def test_get_symbols(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        symbols = schematic_ops.get_symbols(sample_schematic_path)
        assert len(symbols) >= 2
        refs = {s.get("reference") for s in symbols}
        assert "R1" in refs
        assert "U1" in refs

    def test_schematic_info(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        result = schematic_ops.read_schematic(sample_schematic_path)
        info = result["info"]
        assert info["num_symbols"] >= 2
        assert info["num_wires"] >= 2

    def test_add_wire(self, schematic_ops: FileSchematicOps, tmp_path: Path, sample_schematic_path: Path):
        # Copy fixture to temp location for modification
        import shutil
        tmp_sch = tmp_path / "test.kicad_sch"
        shutil.copy2(str(sample_schematic_path), str(tmp_sch))

        result = schematic_ops.add_wire(tmp_sch, 10, 20, 30, 40)
        assert result["start"]["x"] == 10
        assert result["end"]["x"] == 30

        # Verify the wire was added to the file
        content = tmp_sch.read_text(encoding="utf-8")
        assert "10 20" in content and "30 40" in content

    def test_add_label(self, schematic_ops: FileSchematicOps, tmp_path: Path, sample_schematic_path: Path):
        import shutil
        tmp_sch = tmp_path / "test.kicad_sch"
        shutil.copy2(str(sample_schematic_path), str(tmp_sch))

        result = schematic_ops.add_label(tmp_sch, "TEST_NET", 50, 60)
        assert result["text"] == "TEST_NET"
