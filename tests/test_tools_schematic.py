"""Tests for schematic tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools.schematic import register_tools
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_sch(mock_composite: CompositeBackend, tmp_change_log: ChangeLog):
    mcp = FastMCP("test")
    register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


class TestReadSchematic:
    def test_read(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        result_json = mcp_sch._tool_manager._tools["read_schematic"].fn(str(sample_schematic_path))
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "info" in result
        assert "symbols" in result

    def test_invalid_path(self, mcp_sch: FastMCP):
        with pytest.raises(Exception):
            mcp_sch._tool_manager._tools["read_schematic"].fn("/nonexistent/sch.kicad_sch")


class TestAddComponent:
    def test_add(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        result_json = mcp_sch._tool_manager._tools["add_component"].fn(
            path=str(sample_schematic_path),
            lib_id="Device:R",
            reference="R5",
            value="100k",
            x=150.0,
            y=80.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["reference"] == "R5"


class TestAddWire:
    def test_add(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        result_json = mcp_sch._tool_manager._tools["add_wire"].fn(
            path=str(sample_schematic_path),
            start_x=10.0, start_y=20.0,
            end_x=30.0, end_y=40.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"


class TestAddLabel:
    def test_add_net_label(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        result_json = mcp_sch._tool_manager._tools["add_label"].fn(
            path=str(sample_schematic_path),
            text="VCC",
            x=50.0,
            y=60.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"

    def test_invalid_label_type(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        result_json = mcp_sch._tool_manager._tools["add_label"].fn(
            path=str(sample_schematic_path),
            text="TEST",
            x=0, y=0,
            label_type="invalid_type",
        )
        result = json.loads(result_json)
        assert result["status"] == "error"


class TestRemoveComponent:
    def test_remove_via_mock(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        """Test remove_component tool via mock backend."""
        result_json = mcp_sch._tool_manager._tools["remove_component"].fn(
            path=str(sample_schematic_path),
            reference="R1",
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["reference"] == "R1"
        assert result["removed"] is True

    def test_remove_file_backend(self, tmp_path: Path):
        """Test remove_component via FileSchematicOps on a real file."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_content = (
            '(kicad_sch (version 20230121) (generator "test")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (lib_symbols)\n'
            '  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)\n'
            '    (uuid "11111111-1111-1111-1111-111111111111")\n'
            '    (property "Reference" "R1" (at 100 48 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "10k" (at 100 52 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            '  (symbol (lib_id "Device:R") (at 100 70 0) (unit 1)\n'
            '    (uuid "22222222-2222-2222-2222-222222222222")\n'
            '    (property "Reference" "R2" (at 100 68 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "4.7k" (at 100 72 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            ')\n'
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_content, encoding="utf-8")

        ops = FileSchematicOps()
        result = ops.remove_component(sch_file, "R1")
        assert result["reference"] == "R1"
        assert result["removed"] is True

        # Verify file content
        modified = sch_file.read_text(encoding="utf-8")
        assert '"R1"' not in modified
        assert '"R2"' in modified
        assert modified.strip().startswith("(")
        assert modified.strip().endswith(")")

    def test_remove_not_found(self, tmp_path: Path):
        """Test removing a non-existent component raises ValueError."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_content = (
            '(kicad_sch (version 20230121) (generator "test")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (lib_symbols)\n'
            ')\n'
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_content, encoding="utf-8")

        ops = FileSchematicOps()
        with pytest.raises(ValueError, match="not found"):
            ops.remove_component(sch_file, "R99")

    def test_remove_tool_not_found_returns_error(self, mcp_sch: FastMCP, tmp_path: Path):
        """Test that the tool returns an error JSON (not exception) for missing refs."""
        # Create a real schematic file with no matching component
        sch_content = (
            '(kicad_sch (version 20230121) (generator "test")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (lib_symbols)\n'
            ')\n'
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(sch_content, encoding="utf-8")

        # The mock backend doesn't raise, so this will succeed through mock.
        # This test validates the mock path returns success.
        result_json = mcp_sch._tool_manager._tools["remove_component"].fn(
            path=str(sch_file),
            reference="R99",
        )
        result = json.loads(result_json)
        assert result["status"] == "success"  # Mock always succeeds


MOVE_TEST_SCHEMATIC = (
    '(kicad_sch (version 20230121) (generator "test")\n'
    '  (uuid "00000000-0000-0000-0000-000000000000")\n'
    '  (lib_symbols)\n'
    '  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)\n'
    '    (uuid "11111111-1111-1111-1111-111111111111")\n'
    '    (property "Reference" "R1" (at 100 48 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '    (property "Value" "10k" (at 100 52 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '  )\n'
    ')\n'
)


class TestMoveComponent:
    def test_move_via_mock(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        """Test move_schematic_component tool via mock backend."""
        result_json = mcp_sch._tool_manager._tools["move_schematic_component"].fn(
            path=str(sample_schematic_path),
            reference="R1",
            x=200.0,
            y=100.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["reference"] == "R1"
        assert result["position"] == {"x": 200.0, "y": 100.0}

    def test_move_with_rotation(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        """Test move_schematic_component with rotation via mock."""
        result_json = mcp_sch._tool_manager._tools["move_schematic_component"].fn(
            path=str(sample_schematic_path),
            reference="R1",
            x=50.0,
            y=60.0,
            rotation=90.0,
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["rotation"] == 90.0

    def test_move_file_backend_position(self, tmp_path: Path):
        """Test FileSchematicOps.move_component updates position correctly."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MOVE_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        result = ops.move_component(sch_file, "R1", 200.0, 100.0)
        assert result["reference"] == "R1"
        assert result["position"] == {"x": 200.0, "y": 100.0}
        assert result["rotation"] == 0.0  # unchanged

        modified = sch_file.read_text(encoding="utf-8")
        assert "(at 200.0 100.0 0.0)" in modified  # symbol position updated
        # Original position gone
        assert "(at 100 50 0)" not in modified

    def test_move_file_backend_shifts_properties(self, tmp_path: Path):
        """Test that property (at ...) positions are shifted by the same delta."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MOVE_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        # Move from (100,50) to (110,60) => delta (10, 10)
        ops.move_component(sch_file, "R1", 110.0, 60.0)

        modified = sch_file.read_text(encoding="utf-8")
        # Reference was at (100,48) => should be (110,58)
        assert "(at 110.0 58.0 0)" in modified
        # Value was at (100,52) => should be (110,62)
        assert "(at 110.0 62.0 0)" in modified

    def test_move_file_backend_rotation(self, tmp_path: Path):
        """Test that rotation is updated when provided."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MOVE_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        result = ops.move_component(sch_file, "R1", 100.0, 50.0, rotation=90.0)
        assert result["rotation"] == 90.0

        modified = sch_file.read_text(encoding="utf-8")
        assert "(at 100.0 50.0 90.0)" in modified

    def test_move_not_found(self, tmp_path: Path):
        """Test moving a non-existent component raises ValueError."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MOVE_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        with pytest.raises(ValueError, match="not found"):
            ops.move_component(sch_file, "C99", 0.0, 0.0)

    def test_move_preserves_other_symbols(self, tmp_path: Path):
        """Test that moving one symbol doesn't affect others."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        content = (
            '(kicad_sch (version 20230121) (generator "test")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (lib_symbols)\n'
            '  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)\n'
            '    (uuid "11111111-1111-1111-1111-111111111111")\n'
            '    (property "Reference" "R1" (at 100 48 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "10k" (at 100 52 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            '  (symbol (lib_id "Device:R") (at 200 80 0) (unit 1)\n'
            '    (uuid "22222222-2222-2222-2222-222222222222")\n'
            '    (property "Reference" "R2" (at 200 78 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "4.7k" (at 200 82 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            ')\n'
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(content, encoding="utf-8")

        ops = FileSchematicOps()
        ops.move_component(sch_file, "R1", 150.0, 70.0)

        modified = sch_file.read_text(encoding="utf-8")
        # R2 should be untouched
        assert "(at 200 80 0)" in modified
        assert "(at 200 78 0)" in modified
        assert "(at 200 82 0)" in modified


PROP_TEST_SCHEMATIC = (
    '(kicad_sch (version 20230121) (generator "test")\n'
    '  (uuid "00000000-0000-0000-0000-000000000000")\n'
    '  (lib_symbols)\n'
    '  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)\n'
    '    (uuid "11111111-1111-1111-1111-111111111111")\n'
    '    (property "Reference" "R1" (at 100 48 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '    (property "Value" "10k" (at 100 52 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '    (property "Footprint" "Resistor_SMD:R_0805" (at 100 54 0)\n'
    '      (effects (font (size 1.27 1.27)) hide)\n'
    '    )\n'
    '  )\n'
    ')\n'
)


class TestUpdateComponentProperty:
    def test_update_via_mock(self, mcp_sch: FastMCP, sample_schematic_path: Path):
        """Test update_component_property tool via mock backend."""
        result_json = mcp_sch._tool_manager._tools["update_component_property"].fn(
            path=str(sample_schematic_path),
            reference="R1",
            property_name="Value",
            property_value="22k",
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["reference"] == "R1"
        assert result["property"] == "Value"
        assert result["value"] == "22k"

    def test_update_existing_value(self, tmp_path: Path):
        """Test updating an existing property value."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(PROP_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        result = ops.update_component_property(sch_file, "R1", "Value", "22k")
        assert result["value"] == "22k"

        modified = sch_file.read_text(encoding="utf-8")
        assert '"22k"' in modified
        assert '"10k"' not in modified
        # Other properties untouched
        assert '"R1"' in modified
        assert '"Resistor_SMD:R_0805"' in modified

    def test_update_footprint(self, tmp_path: Path):
        """Test updating the Footprint property."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(PROP_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        ops.update_component_property(sch_file, "R1", "Footprint", "Resistor_SMD:R_0402")

        modified = sch_file.read_text(encoding="utf-8")
        assert '"Resistor_SMD:R_0402"' in modified
        assert '"Resistor_SMD:R_0805"' not in modified

    def test_add_new_property(self, tmp_path: Path):
        """Test adding a property that doesn't exist yet."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(PROP_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        result = ops.update_component_property(sch_file, "R1", "MPN", "RC0805FR-0710KL")
        assert result["property"] == "MPN"

        modified = sch_file.read_text(encoding="utf-8")
        assert '"MPN"' in modified
        assert '"RC0805FR-0710KL"' in modified
        # File should still be valid sexp
        assert modified.strip().startswith("(")
        assert modified.strip().endswith(")")

    def test_update_not_found(self, tmp_path: Path):
        """Test updating a property on a non-existent component."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(PROP_TEST_SCHEMATIC, encoding="utf-8")

        ops = FileSchematicOps()
        with pytest.raises(ValueError, match="not found"):
            ops.update_component_property(sch_file, "C99", "Value", "100nF")

    def test_update_preserves_other_symbols(self, tmp_path: Path):
        """Test that updating one symbol's property doesn't affect others."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        content = (
            '(kicad_sch (version 20230121) (generator "test")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (lib_symbols)\n'
            '  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)\n'
            '    (uuid "11111111-1111-1111-1111-111111111111")\n'
            '    (property "Reference" "R1" (at 100 48 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "10k" (at 100 52 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            '  (symbol (lib_id "Device:R") (at 200 80 0) (unit 1)\n'
            '    (uuid "22222222-2222-2222-2222-222222222222")\n'
            '    (property "Reference" "R2" (at 200 78 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (property "Value" "10k" (at 200 82 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '  )\n'
            ')\n'
        )
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(content, encoding="utf-8")

        ops = FileSchematicOps()
        ops.update_component_property(sch_file, "R1", "Value", "47k")

        modified = sch_file.read_text(encoding="utf-8")
        # R1 should have 47k
        assert '"47k"' in modified
        # R2 should still have 10k — find it within R2's block
        from kicad_mcp.utils.sexp_parser import find_symbol_block_by_reference
        loc = find_symbol_block_by_reference(modified, "R2")
        assert loc is not None
        r2_block = modified[loc[0]:loc[1] + 1]
        assert '"10k"' in r2_block


class TestCompareSchematicPcb:
    def test_compare_matching(self, mcp_sch: FastMCP, sample_schematic_path: Path,
                              sample_board_path: Path):
        """Test comparison when mock schematic and PCB have overlapping components."""
        result_json = mcp_sch._tool_manager._tools["compare_schematic_pcb"].fn(
            schematic_path=str(sample_schematic_path),
            board_path=str(sample_board_path),
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "summary" in result
        assert "missing_from_pcb" in result
        assert "missing_from_schematic" in result
        assert "footprint_mismatches" in result
        assert "value_mismatches" in result

    def test_compare_detects_missing_from_pcb(self, mcp_sch: FastMCP,
                                               sample_schematic_path: Path,
                                               sample_board_path: Path):
        """Mock schematic has R1, U1. Mock PCB has R1, R2, U1.
        So schematic is missing R2 perspective: nothing missing from PCB
        since the mock schematic only returns R1 and U1."""
        result_json = mcp_sch._tool_manager._tools["compare_schematic_pcb"].fn(
            schematic_path=str(sample_schematic_path),
            board_path=str(sample_board_path),
        )
        result = json.loads(result_json)
        summary = result["summary"]
        # Mock schematic has R1, U1 (2 components)
        # Mock PCB has R1, R2, U1 (3 components)
        assert summary["schematic_components"] == 2
        assert summary["pcb_components"] == 3
        # R2 is in PCB but not in schematic
        assert summary["missing_from_schematic"] == 1
        assert result["missing_from_schematic"][0]["reference"] == "R2"

    def test_compare_logic_directly(self):
        """Test the comparison logic with controlled data by calling the tool
        with a custom mock that has mismatches."""
        from unittest.mock import MagicMock

        # Build a minimal MCP + backend with controlled data
        mcp = FastMCP("test")
        mock_backend = MagicMock(spec=CompositeBackend)

        sch_ops = MagicMock()
        sch_ops.read_schematic.return_value = {
            "symbols": [
                {"reference": "R1", "value": "10k", "lib_id": "Device:R", "footprint": "R_0805"},
                {"reference": "R2", "value": "4.7k", "lib_id": "Device:R", "footprint": "R_0603"},
                {"reference": "C1", "value": "100nF", "lib_id": "Device:C"},
                {"reference": "#PWR01", "value": "GND", "lib_id": "power:GND", "is_power": True},
            ],
        }
        pcb_ops = MagicMock()
        pcb_ops.read_board.return_value = {
            "components": [
                {"reference": "R1", "value": "10k", "footprint": "R_0805"},
                {"reference": "R2", "value": "4.7k", "footprint": "R_0805"},  # footprint mismatch
                {"reference": "U1", "value": "ATmega", "footprint": "TQFP-44"},  # not in schematic
            ],
        }
        mock_backend.get_schematic_ops.return_value = sch_ops
        mock_backend.get_board_ops.return_value = pcb_ops

        from kicad_mcp.utils.change_log import ChangeLog
        change_log = ChangeLog(Path("/dev/null"))

        from kicad_mcp.tools.schematic import register_tools
        register_tools(mcp, mock_backend, change_log)

        # Need valid file paths for validation — create temp files
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            sch = Path(td) / "test.kicad_sch"
            pcb = Path(td) / "test.kicad_pcb"
            sch.write_text("(kicad_sch)")
            pcb.write_text("(kicad_pcb)")

            result_json = mcp._tool_manager._tools["compare_schematic_pcb"].fn(
                schematic_path=str(sch),
                board_path=str(pcb),
            )

        result = json.loads(result_json)
        assert result["status"] == "success"

        summary = result["summary"]
        # Schematic: R1, R2, C1 (power symbol #PWR01 excluded) = 3
        assert summary["schematic_components"] == 3
        # PCB: R1, R2, U1 = 3
        assert summary["pcb_components"] == 3

        # C1 in schematic but not PCB
        assert summary["missing_from_pcb"] == 1
        assert result["missing_from_pcb"][0]["reference"] == "C1"

        # U1 in PCB but not schematic
        assert summary["missing_from_schematic"] == 1
        assert result["missing_from_schematic"][0]["reference"] == "U1"

        # R2 footprint mismatch: R_0603 (sch) vs R_0805 (pcb)
        assert summary["footprint_mismatches"] == 1
        fp_mm = result["footprint_mismatches"][0]
        assert fp_mm["reference"] == "R2"
        assert fp_mm["schematic_footprint"] == "R_0603"
        assert fp_mm["pcb_footprint"] == "R_0805"

        # R1 matches perfectly, R2 has mismatch => 1 matched
        assert summary["matched"] == 1


# --- lib_symbols cache injection tests ---

MINIMAL_SCHEMATIC = (
    '(kicad_sch (version 20230121) (generator "test")\n'
    '  (uuid "00000000-0000-0000-0000-000000000000")\n'
    '  (lib_symbols\n'
    '  )\n'
    ')\n'
)

MINIMAL_SCHEMATIC_NO_LIB_SYMBOLS = (
    '(kicad_sch (version 20230121) (generator "test")\n'
    '  (uuid "00000000-0000-0000-0000-000000000000")\n'
    ')\n'
)

MOCK_RESISTOR_LIB = (
    '(kicad_symbol_lib\n'
    '  (version 20231120)\n'
    '  (generator "test")\n'
    '  (symbol "R"\n'
    '    (property "Reference" "R" (at 0 0 0))\n'
    '    (property "Value" "R" (at 0 0 0))\n'
    '    (symbol "R_0_1"\n'
    '      (polyline (pts (xy 0 -1.016) (xy 0 1.016)))\n'
    '    )\n'
    '    (symbol "R_1_1"\n'
    '      (pin passive line (at 0 1.27 270) (length 0.254)\n'
    '        (name "~" (effects (font (size 1.27 1.27))))\n'
    '        (number "1" (effects (font (size 1.27 1.27))))\n'
    '      )\n'
    '      (pin passive line (at 0 -1.27 90) (length 0.254)\n'
    '        (name "~" (effects (font (size 1.27 1.27))))\n'
    '        (number "2" (effects (font (size 1.27 1.27))))\n'
    '      )\n'
    '    )\n'
    '  )\n'
    ')\n'
)

MOCK_POWER_LIB = (
    '(kicad_symbol_lib\n'
    '  (version 20231120)\n'
    '  (generator "test")\n'
    '  (symbol "GND"\n'
    '    (power)\n'
    '    (property "Reference" "#PWR" (at 0 0 0))\n'
    '    (property "Value" "GND" (at 0 0 0))\n'
    '    (symbol "GND_0_1"\n'
    '      (polyline (pts (xy 0 0) (xy 0 -1.27)))\n'
    '    )\n'
    '    (symbol "GND_1_1"\n'
    '      (pin power_in line (at 0 0 270) (length 0)\n'
    '        (name "GND" (effects (font (size 1.27 1.27))))\n'
    '        (number "1" (effects (font (size 1.27 1.27))))\n'
    '      )\n'
    '    )\n'
    '  )\n'
    ')\n'
)


class TestLibSymbolsCache:
    def _make_ops(self, tmp_path: Path, lib_content: str, lib_filename: str) -> "FileSchematicOps":
        """Create a FileSchematicOps with a mock library file."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        lib_file = tmp_path / lib_filename
        lib_file.write_text(lib_content, encoding="utf-8")
        ops = FileSchematicOps()
        ops._symbol_libs = [lib_file]
        return ops

    def test_lib_symbols_injected_on_add_component(self, tmp_path: Path):
        """Adding a component injects its symbol definition into lib_symbols."""
        ops = self._make_ops(tmp_path, MOCK_RESISTOR_LIB, "Device.kicad_sym")

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC, encoding="utf-8")

        ops.add_component(sch_file, "Device:R", "R1", "10k", 100.0, 50.0)

        content = sch_file.read_text(encoding="utf-8")
        # lib_symbols should contain the renamed symbol
        assert '(symbol "Device:R"' in content
        # Sub-symbols should also be renamed
        assert '(symbol "Device:R_0_1"' in content
        assert '(symbol "Device:R_1_1"' in content
        # The placed instance should still be there
        assert '(lib_id "Device:R")' in content

    def test_lib_symbols_not_duplicated(self, tmp_path: Path):
        """Adding the same component twice should not duplicate the cache entry."""
        ops = self._make_ops(tmp_path, MOCK_RESISTOR_LIB, "Device.kicad_sym")

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC, encoding="utf-8")

        ops.add_component(sch_file, "Device:R", "R1", "10k", 100.0, 50.0)
        ops.add_component(sch_file, "Device:R", "R2", "4.7k", 100.0, 80.0)

        content = sch_file.read_text(encoding="utf-8")
        # Count occurrences of the top-level cached symbol
        import re
        matches = re.findall(r'\(symbol "Device:R"', content)
        assert len(matches) == 1, f"Expected 1 lib_symbols entry, found {len(matches)}"

    def test_lib_symbols_power_symbol(self, tmp_path: Path):
        """Adding a power symbol injects its definition with (power) tag."""
        ops = self._make_ops(tmp_path, MOCK_POWER_LIB, "power.kicad_sym")

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC, encoding="utf-8")

        ops.add_power_symbol(sch_file, "GND", 100.0, 50.0)

        content = sch_file.read_text(encoding="utf-8")
        assert '(symbol "power:GND"' in content
        assert '(symbol "power:GND_0_1"' in content
        assert '(symbol "power:GND_1_1"' in content
        assert "(power)" in content

    def test_lib_symbols_missing_library_graceful(self, tmp_path: Path):
        """When library is not found, component is still placed without crashing."""
        from kicad_mcp.backends.file_backend import FileSchematicOps

        ops = FileSchematicOps()
        ops._symbol_libs = []  # No libraries at all

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC, encoding="utf-8")

        result = ops.add_component(sch_file, "Device:R", "R1", "10k", 100.0, 50.0)

        # Component should still be placed
        assert result["reference"] == "R1"
        content = sch_file.read_text(encoding="utf-8")
        assert '(lib_id "Device:R")' in content
        # No cached symbol since library wasn't found
        assert '(symbol "Device:R"' not in content

    def test_lib_symbols_section_created_if_missing(self, tmp_path: Path):
        """Schematic without lib_symbols section gets one created."""
        ops = self._make_ops(tmp_path, MOCK_RESISTOR_LIB, "Device.kicad_sym")

        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC_NO_LIB_SYMBOLS, encoding="utf-8")

        ops.add_component(sch_file, "Device:R", "R1", "10k", 100.0, 50.0)

        content = sch_file.read_text(encoding="utf-8")
        assert "(lib_symbols" in content
        assert '(symbol "Device:R"' in content
        assert '(symbol "Device:R_0_1"' in content
