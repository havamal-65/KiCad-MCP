"""Tests for export tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools.export import register_tools
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_export(mock_composite: CompositeBackend, tmp_change_log: ChangeLog):
    mcp = FastMCP("test")
    register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


class TestExportGerbers:
    def test_export(self, mcp_export: FastMCP, sample_board_path: Path, tmp_path: Path):
        result_json = mcp_export._tool_manager._tools["export_gerbers"].fn(
            path=str(sample_board_path),
            output_dir=str(tmp_path / "gerbers"),
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
        assert result["success"] is True


class TestExportBOM:
    def test_export_csv(self, mcp_export: FastMCP, sample_board_path: Path, tmp_path: Path):
        result_json = mcp_export._tool_manager._tools["export_bom"].fn(
            path=str(sample_board_path),
            output=str(tmp_path / "bom.csv"),
            format="csv",
        )
        result = json.loads(result_json)
        assert result["status"] == "success"

    def test_invalid_format(self, mcp_export: FastMCP, sample_board_path: Path, tmp_path: Path):
        result_json = mcp_export._tool_manager._tools["export_bom"].fn(
            path=str(sample_board_path),
            output=str(tmp_path / "bom.txt"),
            format="invalid",
        )
        result = json.loads(result_json)
        assert result["status"] == "error"


class TestExportPDF:
    def test_export(self, mcp_export: FastMCP, sample_board_path: Path, tmp_path: Path):
        result_json = mcp_export._tool_manager._tools["export_pdf"].fn(
            path=str(sample_board_path),
            output=str(tmp_path / "board.pdf"),
        )
        result = json.loads(result_json)
        assert result["status"] == "success"
