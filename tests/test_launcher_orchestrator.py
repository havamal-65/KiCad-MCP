"""Unit tests for launcher.orchestrator.plan_bringup (M2 / REQ-TEST-002)."""

from __future__ import annotations

from pathlib import Path

from launcher.orchestrator import plan_bringup


def _actions(steps):
    return {s.piece: s.action for s in steps}


BOARD = Path("C:/proj/board.kicad_pcb")


def test_all_down_starts_all_three():
    steps = plan_bringup({"pcbnew_running": False, "mcp_state": "down"}, BOARD)
    assert _actions(steps) == {"kicad": "start", "mcp": "start", "claude": "start"}


def test_pcbnew_running_skips_kicad():
    steps = plan_bringup({"pcbnew_running": True, "mcp_state": "down"}, BOARD)
    assert _actions(steps)["kicad"] == "skip"
    assert _actions(steps)["mcp"] == "start"


def test_mcp_ours_skips_mcp():
    steps = plan_bringup({"pcbnew_running": False, "mcp_state": "ours"}, BOARD)
    assert _actions(steps)["mcp"] == "skip"


def test_mcp_foreign_does_not_start_second():
    steps = plan_bringup({"pcbnew_running": False, "mcp_state": "foreign"}, BOARD)
    assert _actions(steps)["mcp"] == "skip"
    assert "foreign" in next(s.reason for s in steps if s.piece == "mcp")


def test_board_none_skips_kicad_but_still_plans():
    steps = plan_bringup({"pcbnew_running": False, "mcp_state": "down"}, None)
    acts = _actions(steps)
    assert acts["kicad"] == "skip"
    assert acts["mcp"] == "start"
    assert acts["claude"] == "start"


def test_claude_always_starts():
    for pcb in (True, False):
        for mcp in ("ours", "foreign", "down"):
            steps = plan_bringup({"pcbnew_running": pcb, "mcp_state": mcp}, BOARD)
            assert _actions(steps)["claude"] == "start"


def test_missing_status_keys_default_to_down():
    steps = plan_bringup({}, BOARD)
    acts = _actions(steps)
    assert acts["kicad"] == "start"
    assert acts["mcp"] == "start"
