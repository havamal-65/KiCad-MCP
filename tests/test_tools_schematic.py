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
