"""Tests for board tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools.board import register_tools
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_board(mock_composite: CompositeBackend, tmp_change_log: ChangeLog):
    mcp = FastMCP("test")
    register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


class TestReadBoard:
    def test_read_board(self, mcp_board: FastMCP, sample_board_path: Path):
        result_json = mcp_board._tool_manager._tools["read_board"].fn(str(sample_board_path))
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "info" in result
        assert "components" in result
        assert "nets" in result

    def test_read_board_invalid_path(self, mcp_board: FastMCP):
        with pytest.raises(Exception):
            mcp_board._tool_manager._tools["read_board"].fn("/nonexistent/board.kicad_pcb")


class TestGetBoardInfo:
    def test_get_info(self, mcp_board: FastMCP, sample_board_path: Path):
        result_json = mcp_board._tool_manager._tools["get_board_info"].fn(str(sample_board_path))
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "info" in result


class TestPlaceComponent:
    def test_place(self, mcp_board: FastMCP, sample_board_path: Path):
        result_json = mcp_board._tool_manager._tools["place_component"].fn(
            path=str(sample_board_path),
            reference="R3",
            footprint="Resistor_SMD:R_0805",
            x=100.0,
            y=100.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["reference"] == "R3"

    def test_invalid_reference(self, mcp_board: FastMCP, sample_board_path: Path):
        with pytest.raises(Exception):
            mcp_board._tool_manager._tools["place_component"].fn(
                path=str(sample_board_path),
                reference="123",
                footprint="R_0805",
                x=0, y=0,
            )


class TestAddTrack:
    def test_add_track(self, mcp_board: FastMCP, sample_board_path: Path):
        result_json = mcp_board._tool_manager._tools["add_track"].fn(
            path=str(sample_board_path),
            start_x=10.0, start_y=20.0,
            end_x=30.0, end_y=40.0,
            width=0.25,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"


class TestGetDesignRules:
    def test_get_rules(self, mcp_board: FastMCP, sample_board_path: Path):
        result_json = mcp_board._tool_manager._tools["get_design_rules"].fn(str(sample_board_path))
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "rules" in result


# --- File backend board modification tests ---

MINIMAL_PCB = (
    '(kicad_pcb (version 20240108) (generator "test")\n'
    '  (net 0 "")\n'
    '  (net 1 "VCC")\n'
    '  (net 2 "GND")\n'
    '  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")\n'
    '    (at 100 50)\n'
    '    (property "Reference" "R1" (at 0 0 0)\n'
    '      (effects (font (size 1 1) (thickness 0.15)))\n'
    '    )\n'
    '    (property "Value" "10k" (at 0 0 0)\n'
    '      (effects (font (size 1 1) (thickness 0.15)))\n'
    '    )\n'
    '    (pad "1" smd roundrect (at -1 0) (size 1 1.2) (layers "F.Cu"))\n'
    '    (pad "2" smd roundrect (at 1 0) (size 1 1.2) (layers "F.Cu"))\n'
    '  )\n'
    ')\n'
)


class TestFileBoardOps:
    def test_place_component(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.place_component(pcb_file, "R2", "Resistor_SMD:R_0402", 120.0, 60.0)
        assert result["reference"] == "R2"
        assert result["footprint"] == "Resistor_SMD:R_0402"
        assert result["position"] == {"x": 120.0, "y": 60.0}
        assert "uuid" in result

        content = pcb_file.read_text(encoding="utf-8")
        assert '"R2"' in content
        assert '"Resistor_SMD:R_0402"' in content
        assert content.strip().endswith(")")

    def test_move_component(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.move_component(pcb_file, "R1", 150.0, 80.0)
        assert result["reference"] == "R1"
        assert result["position"] == {"x": 150.0, "y": 80.0}

        content = pcb_file.read_text(encoding="utf-8")
        assert "(at 150.0 80.0)" in content
        assert "(at 100 50)" not in content

    def test_move_component_with_rotation(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.move_component(pcb_file, "R1", 100.0, 50.0, rotation=90.0)
        assert result["rotation"] == 90.0

        content = pcb_file.read_text(encoding="utf-8")
        assert "(at 100.0 50.0 90.0)" in content

    def test_move_component_not_found(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        with pytest.raises(ValueError, match="not found"):
            ops.move_component(pcb_file, "C99", 0.0, 0.0)

    def test_add_track(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.add_track(pcb_file, 10.0, 20.0, 30.0, 40.0, 0.25, "F.Cu", "VCC")
        assert result["start"] == {"x": 10.0, "y": 20.0}
        assert result["end"] == {"x": 30.0, "y": 40.0}
        assert result["width"] == 0.25
        assert result["net"] == "VCC"
        assert "uuid" in result

        content = pcb_file.read_text(encoding="utf-8")
        assert "(segment" in content
        assert "(net 1)" in content  # VCC is net 1

    def test_add_track_new_net(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.add_track(pcb_file, 10.0, 20.0, 30.0, 40.0, 0.25, "F.Cu", "SDA")

        content = pcb_file.read_text(encoding="utf-8")
        assert '(net 3 "SDA")' in content  # New net assigned ID 3
        assert "(net 3)" in content

    def test_add_via(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.add_via(pcb_file, 50.0, 60.0, 0.8, 0.4, "GND")
        assert result["position"] == {"x": 50.0, "y": 60.0}
        assert result["net"] == "GND"
        assert "uuid" in result

        content = pcb_file.read_text(encoding="utf-8")
        assert "(via" in content
        assert "(net 2)" in content  # GND is net 2

    def test_assign_net(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        result = ops.assign_net(pcb_file, "R1", "1", "VCC")
        assert result["reference"] == "R1"
        assert result["pad"] == "1"
        assert result["net"] == "VCC"

        content = pcb_file.read_text(encoding="utf-8")
        assert '(net 1 "VCC")' in content

    def test_assign_net_not_found(self, tmp_path: Path):
        from kicad_mcp.backends.file_backend import FileBoardOps

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB, encoding="utf-8")

        ops = FileBoardOps()
        with pytest.raises(ValueError, match="not found"):
            ops.assign_net(pcb_file, "C99", "1", "VCC")
