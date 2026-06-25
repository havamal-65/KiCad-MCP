"""Tests for the §6.5 auto_place board-size gate (REQ-GATE-001/003).

auto_place hard-refuses when verify_board_size recorded a FAILURE for the
current board content, proceeds cleanly after a recorded pass, and proceeds with
a board_size_unverified warning when the board was never verified.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.tools import board
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation_cache import record_validation


def _board_file(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    return p


def _auto_place_fn(backend, tmp_path: Path):
    mcp = fastmcp.FastMCP("test")
    board.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "auto_place"
    )


def _proceeding_backend() -> MagicMock:
    backend = MagicMock()
    ops = backend.get_board_modify_ops.return_value
    ops.get_components.return_value = [{"reference": "R1"}]
    ops.auto_place.return_value = {"total_area_mm2": 10.0, "placements": []}
    return backend


_FAIL_RECORD = {
    "passed": False,
    "shortfall_breakdown": {
        "required_mm2": 500.0, "usable_mm2": 196.0, "shortfall_mm2": 304.0,
        "largest_part": {"ref": "U1", "width_mm": 10.0, "height_mm": 10.0},
    },
    "suggested_min_dimensions": {"width_mm": 45.0, "height_mm": 35.0},
}


def test_auto_place_refuses_on_recorded_failure(tmp_path: Path):
    p = _board_file(tmp_path)
    record_validation(p, "verify_board_size", _FAIL_RECORD)
    backend = _proceeding_backend()

    tool_fn = _auto_place_fn(backend, tmp_path)
    result = json.loads(tool_fn(str(p)))

    assert result["status"] == "blocked", result
    assert result["reason"] == "verify_board_size_gate"
    assert result["shortfall_breakdown"]["shortfall_mm2"] == 304.0
    assert result["suggested_min_dimensions"]["width_mm"] == 45.0
    # The gate fires before any placement work.
    backend.get_board_modify_ops.assert_not_called()


def test_auto_place_proceeds_after_recorded_pass(tmp_path: Path):
    p = _board_file(tmp_path)
    record_validation(p, "verify_board_size", {"passed": True})
    backend = _proceeding_backend()

    tool_fn = _auto_place_fn(backend, tmp_path)
    result = json.loads(tool_fn(str(p)))

    assert result["status"] == "success", result
    backend.get_board_modify_ops.return_value.auto_place.assert_called_once()
    warn_types = [w.get("type") for w in result.get("warnings", []) if isinstance(w, dict)]
    assert "board_size_unverified" not in warn_types


def test_auto_place_warns_when_unverified(tmp_path: Path):
    p = _board_file(tmp_path)  # no verify_board_size record
    backend = _proceeding_backend()

    tool_fn = _auto_place_fn(backend, tmp_path)
    result = json.loads(tool_fn(str(p)))

    assert result["status"] == "success", result
    warn_types = [w.get("type") for w in result.get("warnings", []) if isinstance(w, dict)]
    assert "board_size_unverified" in warn_types, result.get("warnings")
