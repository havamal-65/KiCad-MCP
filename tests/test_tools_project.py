"""Tests for project management tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools.project import register_tools
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_with_tools(mock_composite: CompositeBackend, tmp_change_log: ChangeLog):
    mcp = FastMCP("test")
    register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


class TestOpenProject:
    def test_open_project_file(self, sample_project_path: Path, mock_composite, tmp_change_log):
        mcp = FastMCP("test")
        register_tools(mcp, mock_composite, tmp_change_log)

        # Call the tool function directly
        from kicad_mcp.tools.project import register_tools as _reg
        # We need to test the registered function
        result_json = mcp._tool_manager._tools["open_project"].fn(str(sample_project_path))
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "project" in result
        assert result["project"]["name"] == "sample_project"

    def test_open_project_directory(self, fixtures_dir: Path, mock_composite, tmp_change_log):
        mcp = FastMCP("test")
        register_tools(mcp, mock_composite, tmp_change_log)

        result_json = mcp._tool_manager._tools["open_project"].fn(str(fixtures_dir))
        result = json.loads(result_json)
        assert result["status"] == "success"


class TestGetBackendInfo:
    def test_returns_status(self, mock_composite, tmp_change_log):
        mcp = FastMCP("test")
        register_tools(mcp, mock_composite, tmp_change_log)

        result_json = mcp._tool_manager._tools["get_backend_info"].fn()
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert "active_backends" in result
