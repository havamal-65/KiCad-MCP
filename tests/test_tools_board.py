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
