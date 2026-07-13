"""Unit tests for launcher.orchestrator.classify_failures (M2 / REQ-TEST-004)."""

from __future__ import annotations

from pathlib import Path

from launcher.orchestrator import classify_failures, has_blocking

GOOD = {
    "pcbnew_exe": Path("C:/KiCad/bin/pcbnew.exe"),
    "claude_available": True,
    "mcp_state": "down",
    "board": Path("C:/proj/board.kicad_pcb"),
}


def _codes(diags):
    return {d.code for d in diags}


def test_all_good_no_diags():
    assert classify_failures(GOOD) == []


def test_missing_pcbnew_exe():
    diags = classify_failures({**GOOD, "pcbnew_exe": None})
    assert "kicad_missing" in _codes(diags)
    assert has_blocking(diags)


def test_missing_claude_cli():
    diags = classify_failures({**GOOD, "claude_available": False})
    assert "claude_missing" in _codes(diags)
    # Non-blocking: KiCad + MCP can still come up.
    assert not any(d.blocking for d in diags if d.code == "claude_missing")


def test_mcp_foreign():
    diags = classify_failures({**GOOD, "mcp_state": "foreign"})
    assert "port_in_use_foreign" in _codes(diags)


def test_board_none():
    diags = classify_failures({**GOOD, "board": None})
    assert "no_project" in _codes(diags)
    assert has_blocking(diags)


def test_multiple_diags_accumulate():
    diags = classify_failures(
        {"pcbnew_exe": None, "claude_available": False, "mcp_state": "foreign", "board": None}
    )
    assert _codes(diags) == {
        "kicad_missing",
        "claude_missing",
        "port_in_use_foreign",
        "no_project",
    }
