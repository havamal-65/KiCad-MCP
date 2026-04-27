"""Tests for check_courtyard_overlaps (drc.py MCP tool)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture board content builders
# ---------------------------------------------------------------------------

def _board_with_footprints(*footprints: dict) -> str:
    """Build a minimal .kicad_pcb with footprints that have courtyard rectangles.

    Each footprint dict has:
      ref:    reference designator (str)
      at_x:   board X position (float)
      at_y:   board Y position (float)
      w:      courtyard half-width in mm (footprint-relative)
      h:      courtyard half-height in mm (footprint-relative)
    """
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
    ]
    for fp in footprints:
        ref = fp["ref"]
        at_x = fp["at_x"]
        at_y = fp["at_y"]
        w = fp["w"]
        h = fp["h"]
        lines += [
            f'  (footprint "Device:R" (layer "F.Cu") (at {at_x} {at_y})',
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab"))',
            f'    (fp_rect (start {-w} {-h}) (end {w} {h}) (layer "F.CrtYd") (width 0.05))',
            "  )",
        ]
    lines.append(")")
    return "\n".join(lines) + "\n"


@pytest.fixture
def overlapping_board(tmp_path: Path) -> Path:
    """Two footprints whose courtyards overlap by 2 mm in X."""
    content = _board_with_footprints(
        {"ref": "R1", "at_x": 10.0, "at_y": 10.0, "w": 3.0, "h": 2.0},  # x: 7..13
        {"ref": "C1", "at_x": 15.0, "at_y": 10.0, "w": 3.0, "h": 2.0},  # x: 12..18  (overlap: 12..13)
    )
    f = tmp_path / "overlap.kicad_pcb"
    f.write_text(content, encoding="utf-8")
    return f


@pytest.fixture
def non_overlapping_board(tmp_path: Path) -> Path:
    """Two footprints with a 4 mm gap — no overlap."""
    content = _board_with_footprints(
        {"ref": "R1", "at_x": 10.0, "at_y": 10.0, "w": 3.0, "h": 2.0},  # x: 7..13
        {"ref": "C1", "at_x": 20.0, "at_y": 10.0, "w": 3.0, "h": 2.0},  # x: 17..23
    )
    f = tmp_path / "no_overlap.kicad_pcb"
    f.write_text(content, encoding="utf-8")
    return f


@pytest.fixture
def empty_board(tmp_path: Path) -> Path:
    content = "(kicad_pcb\n  (version 20231231)\n)\n"
    f = tmp_path / "empty.kicad_pcb"
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Helper: call the tool function directly
# ---------------------------------------------------------------------------

def _call_tool(board_path: Path) -> dict:
    """Drive the check_courtyard_overlaps logic by instantiating the DRC module."""
    import fastmcp
    from kicad_mcp.utils.change_log import ChangeLog

    # Build a minimal backend stub
    backend_stub = MagicMock()
    change_log = ChangeLog(board_path.parent / "changes.json")

    mcp = fastmcp.FastMCP("test")
    from kicad_mcp.tools import drc
    drc.register_tools(mcp, backend_stub, change_log)

    # Find the registered tool by name and call it
    tool_fn = None
    for tool in mcp._tool_manager._tools.values():
        if tool.name == "check_courtyard_overlaps":
            tool_fn = tool.fn
            break

    assert tool_fn is not None, "check_courtyard_overlaps not registered"
    raw = tool_fn(str(board_path))
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Overlapping case
# ---------------------------------------------------------------------------

def test_overlap_detected(overlapping_board: Path):
    result = _call_tool(overlapping_board)
    assert result["status"] == "success"
    assert result["passed"] is False
    assert result["overlap_count"] >= 1


def test_overlap_has_correct_refs(overlapping_board: Path):
    result = _call_tool(overlapping_board)
    refs_in_overlaps = set()
    for ov in result["overlaps"]:
        refs_in_overlaps.add(ov["ref_a"])
        refs_in_overlaps.add(ov["ref_b"])
    assert "R1" in refs_in_overlaps
    assert "C1" in refs_in_overlaps


def test_overlap_has_positive_dimensions(overlapping_board: Path):
    result = _call_tool(overlapping_board)
    for ov in result["overlaps"]:
        assert ov["overlap_x_mm"] > 0 or ov["overlap_y_mm"] > 0
        assert ov["suggested_move_mm"] > 0


# ---------------------------------------------------------------------------
# Non-overlapping case
# ---------------------------------------------------------------------------

def test_no_overlap_passes(non_overlapping_board: Path):
    result = _call_tool(non_overlapping_board)
    assert result["status"] == "success"
    assert result["passed"] is True
    assert result["overlap_count"] == 0
    assert result["overlaps"] == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_board_passes(empty_board: Path):
    result = _call_tool(empty_board)
    assert result["status"] == "success"
    assert result["passed"] is True
    assert result["overlap_count"] == 0


def test_result_keys_present(non_overlapping_board: Path):
    result = _call_tool(non_overlapping_board)
    assert "status" in result
    assert "passed" in result
    assert "overlap_count" in result
    assert "footprints_checked" in result
    assert "overlaps" in result
