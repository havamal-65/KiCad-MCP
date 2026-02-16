"""Tests for library tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools.library import register_tools
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_lib(mock_composite: CompositeBackend, tmp_change_log: ChangeLog):
    mcp = FastMCP("test")
    register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


class TestSearchSymbols:
    def test_search(self, mcp_lib: FastMCP):
        result_json = mcp_lib._tool_manager._tools["search_symbols"].fn(query="resistor")
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "symbols" in result
        assert result["count"] >= 0


class TestSearchFootprints:
    def test_search(self, mcp_lib: FastMCP):
        result_json = mcp_lib._tool_manager._tools["search_footprints"].fn(query="0805")
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "footprints" in result


class TestListLibraries:
    def test_list(self, mcp_lib: FastMCP):
        result_json = mcp_lib._tool_manager._tools["list_libraries"].fn()
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "libraries" in result
        assert "total" in result


class TestGetSymbolInfo:
    def test_get_info(self, mcp_lib: FastMCP):
        result_json = mcp_lib._tool_manager._tools["get_symbol_info"].fn(lib_id="Device:R")
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["name"] == "R"


class TestGetFootprintInfo:
    def test_get_info(self, mcp_lib: FastMCP):
        result_json = mcp_lib._tool_manager._tools["get_footprint_info"].fn(
            lib_id="Resistor_SMD:R_0805"
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["name"] == "R_0805"


class TestSuggestFootprints:
    def test_suggest_via_mock(self, mcp_lib: FastMCP):
        """Test suggest_footprints tool via mock backend."""
        result_json = mcp_lib._tool_manager._tools["suggest_footprints"].fn(
            lib_id="Device:R"
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["lib_id"] == "Device:R"
        assert "fp_filters" in result
        assert "footprints" in result
        assert len(result["footprints"]) > 0

    def test_suggest_file_backend(self, tmp_path: Path):
        """Test suggest_footprints via FileLibraryOps with crafted data."""
        from kicad_mcp.backends.file_backend import FileLibraryOps

        # Create a symbol library with ki_fp_filters
        sym_lib = tmp_path / "Device.kicad_sym"
        sym_lib.write_text(
            '(kicad_symbol_lib\n'
            '  (version 20231120)\n'
            '  (generator "test")\n'
            '  (symbol "R"\n'
            '    (property "Reference" "R" (at 0 0 0))\n'
            '    (property "Value" "R" (at 0 0 0))\n'
            '    (property "ki_fp_filters" "R_*" (at 0 0 0))\n'
            '    (symbol "R_1_1"\n'
            '      (pin passive line (at 0 1.27 270) (length 0.254)\n'
            '        (name "~" (effects (font (size 1.27 1.27))))\n'
            '        (number "1" (effects (font (size 1.27 1.27))))\n'
            '      )\n'
            '    )\n'
            '  )\n'
            ')\n',
            encoding="utf-8",
        )

        # Create a footprint library with some footprints
        fp_dir = tmp_path / "Resistor_SMD.pretty"
        fp_dir.mkdir()
        (fp_dir / "R_0805.kicad_mod").write_text(
            '(footprint "R_0805" (layer "F.Cu"))\n', encoding="utf-8"
        )
        (fp_dir / "R_0402.kicad_mod").write_text(
            '(footprint "R_0402" (layer "F.Cu"))\n', encoding="utf-8"
        )
        (fp_dir / "SOT-23.kicad_mod").write_text(
            '(footprint "SOT-23" (layer "F.Cu"))\n', encoding="utf-8"
        )

        ops = FileLibraryOps()
        ops._symbol_libs = [sym_lib]
        ops._footprint_libs = [fp_dir]

        result = ops.suggest_footprints("Device:R")
        assert result["lib_id"] == "Device:R"
        assert result["fp_filters"] == ["R_*"]
        names = [fp["name"] for fp in result["footprints"]]
        assert "R_0805" in names
        assert "R_0402" in names
        assert "SOT-23" not in names

    def test_suggest_no_filters(self, tmp_path: Path):
        """Test suggest_footprints when symbol has no fp_filters."""
        from kicad_mcp.backends.file_backend import FileLibraryOps

        sym_lib = tmp_path / "Device.kicad_sym"
        sym_lib.write_text(
            '(kicad_symbol_lib\n'
            '  (version 20231120)\n'
            '  (generator "test")\n'
            '  (symbol "R"\n'
            '    (property "Reference" "R" (at 0 0 0))\n'
            '    (property "Value" "R" (at 0 0 0))\n'
            '  )\n'
            ')\n',
            encoding="utf-8",
        )

        ops = FileLibraryOps()
        ops._symbol_libs = [sym_lib]
        ops._footprint_libs = []

        result = ops.suggest_footprints("Device:R")
        assert result["footprints"] == []
        assert "message" in result
