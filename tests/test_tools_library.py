"""Tests for library tools."""

from __future__ import annotations

import json

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
