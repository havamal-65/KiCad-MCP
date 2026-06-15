"""Unit tests for the #13A open_kicad double-launch guard.

_evaluate_launch_guard is a pure decision function: given the bridge ping
identity, whether a pcbnew editor is already running, and the force flag, it
returns a terminal response dict (reuse-success / refusal) or None ("safe to
launch").  Testing it directly avoids standing up the whole FastMCP server.
"""

from __future__ import annotations

from pathlib import Path

from kicad_mcp_plugin.server import _evaluate_launch_guard

BOARD = Path("/proj/board.kicad_pcb")
OTHER = "/proj/other.kicad_pcb"


def _pcbnew_identity(board_path):
    return {"pong": True, "app": "pcbnew", "pid": 100, "board_path": board_path}


# ---------------------------------------------------------------------------
# Nothing running → proceed
# ---------------------------------------------------------------------------

def test_not_running_proceeds():
    assert _evaluate_launch_guard(BOARD, None, pcbnew_running=False, force=False) is None


# ---------------------------------------------------------------------------
# Bridge up, same board → reuse success
# ---------------------------------------------------------------------------

def test_same_board_reuses_session():
    result = _evaluate_launch_guard(
        BOARD, _pcbnew_identity(str(BOARD)), pcbnew_running=True, force=False
    )
    assert result is not None
    assert result["status"] == "success"
    assert result["bridge"] == "ready"


# ---------------------------------------------------------------------------
# Bridge up, different board → refuse unless force
# ---------------------------------------------------------------------------

def test_other_board_refused():
    result = _evaluate_launch_guard(
        BOARD, _pcbnew_identity(OTHER), pcbnew_running=True, force=False
    )
    assert result is not None
    assert result["status"] == "refused"
    assert result["open_board"] == OTHER
    assert result["requested_board"] == str(BOARD)


def test_other_board_force_proceeds():
    assert _evaluate_launch_guard(
        BOARD, _pcbnew_identity(OTHER), pcbnew_running=True, force=True
    ) is None


# ---------------------------------------------------------------------------
# Bridge up but board unknown → refuse unless force
# ---------------------------------------------------------------------------

def test_unknown_board_refused():
    result = _evaluate_launch_guard(
        BOARD, _pcbnew_identity(None), pcbnew_running=True, force=False
    )
    assert result is not None
    assert result["status"] == "refused"
    assert result["open_board"] is None


# ---------------------------------------------------------------------------
# pcbnew running, bridge down → refuse unless force
# ---------------------------------------------------------------------------

def test_running_no_bridge_refused():
    result = _evaluate_launch_guard(BOARD, None, pcbnew_running=True, force=False)
    assert result is not None
    assert result["status"] == "refused"
    assert result["bridge"] == "down"


def test_running_no_bridge_force_proceeds():
    assert _evaluate_launch_guard(BOARD, None, pcbnew_running=True, force=True) is None


# ---------------------------------------------------------------------------
# Wrong-owner bridge (project manager holding the port)
# ---------------------------------------------------------------------------

def test_wrong_owner_with_no_pcbnew_proceeds():
    # PM holds the port but no pcbnew editor exists → launching pcbnew is safe.
    pm = {"pong": True, "app": "kicad", "pid": 50, "board_path": None}
    assert _evaluate_launch_guard(BOARD, pm, pcbnew_running=False, force=False) is None


def test_wrong_owner_with_pcbnew_running_refused():
    pm = {"pong": True, "app": "kicad", "pid": 50, "board_path": None}
    result = _evaluate_launch_guard(BOARD, pm, pcbnew_running=True, force=False)
    assert result is not None
    assert result["status"] == "refused"


# ---------------------------------------------------------------------------
# Legacy bridge (no app field) with matching board → reuse
# ---------------------------------------------------------------------------

def test_legacy_bridge_same_board_reuses():
    legacy = {"pong": True, "board_path": str(BOARD)}  # no "app"
    result = _evaluate_launch_guard(BOARD, legacy, pcbnew_running=True, force=False)
    assert result is not None
    assert result["status"] == "success"
