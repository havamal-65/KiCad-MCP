"""Tests for save_project — #14C coherence messaging + the S2 live flush.

Since F1/S2 (spec §3 row 19) save_project actually flushes the live board
through the router (IPC-first, bridge fallback) when the .kicad_pcb exists and
a live path serves ``save_board``; otherwise it returns the advisory text. We
register the project tools against a capturing MCP stub and a mocked backend,
then invoke the captured save_project closure directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kicad_mcp.tools import project as project_tools
from kicad_mcp.utils.change_log import ChangeLog


class _CapturingMCP:
    """Minimal stand-in for FastMCP that records @mcp.tool()-decorated fns."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _save_project_fn(backend, tmp_path):
    mcp = _CapturingMCP()
    change_log = ChangeLog(tmp_path / "changes.json")
    project_tools.register_tools(mcp, backend, change_log)
    return mcp.tools["save_project"]


def _backend(*, live: bool, save_error: Exception | None = None):
    backend = MagicMock()
    backend.has_capability.return_value = False
    if live:
        backend.get_active_project.return_value = {"project_name": "demo"}
    else:
        backend.get_active_project.side_effect = RuntimeError("no live session")
    if save_error is not None:
        backend.get_board_modify_ops.return_value.save_board.side_effect = save_error
    else:
        backend.get_board_modify_ops.return_value.save_board.return_value = {
            "success": True}
    return backend


def test_save_project_reports_live_bridge_session(tmp_path):
    fn = _save_project_fn(_backend(live=True), tmp_path)
    out = json.loads(fn("/tmp/demo.kicad_pro"))
    assert out["status"] == "info"
    assert out["live_bridge_session"] is True
    assert "diverge" in out["message"]
    assert "reload_board" in out["message"]
    assert "backend" in out


def test_save_project_no_live_session(tmp_path):
    fn = _save_project_fn(_backend(live=False), tmp_path)
    out = json.loads(fn("/tmp/demo.kicad_pro"))
    assert out["status"] == "info"
    assert out["live_bridge_session"] is False
    # No live session → no divergence warning.
    assert "diverge" not in out["message"]


def _project_with_board(tmp_path):
    """A .kicad_pro with its sibling .kicad_pcb on disk."""
    pro = tmp_path / "demo.kicad_pro"
    pro.write_text("{}", encoding="utf-8")
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    return pro, board


def test_save_project_flushes_live_board_via_router(tmp_path):
    """S2 row 19: an existing board + a serving live path → real save."""
    backend = _backend(live=True)
    fn = _save_project_fn(backend, tmp_path)
    pro, board = _project_with_board(tmp_path)
    out = json.loads(fn(str(pro)))
    assert out["status"] == "success"
    assert out["live_bridge_session"] is True
    backend.get_board_modify_ops.return_value.save_board.assert_called_once_with(board)


def test_save_project_accepts_board_path_directly(tmp_path):
    backend = _backend(live=True)
    fn = _save_project_fn(backend, tmp_path)
    _, board = _project_with_board(tmp_path)
    out = json.loads(fn(str(board)))
    assert out["status"] == "success"
    backend.get_board_modify_ops.return_value.save_board.assert_called_once_with(board)


def test_save_project_no_live_path_falls_to_advisory(tmp_path):
    """save_board NotImplementedError (file ops) → the old advisory text."""
    backend = _backend(live=False, save_error=NotImplementedError("no live path"))
    fn = _save_project_fn(backend, tmp_path)
    pro, _ = _project_with_board(tmp_path)
    out = json.loads(fn(str(pro)))
    assert out["status"] == "info"
    assert "auto-save" in out["message"]


def test_save_project_safe_refuse_falls_to_advisory(tmp_path):
    """KiCad open with no live path → advisory with the divergence warning."""
    from kicad_mcp.models.errors import SafeRefuseError
    backend = _backend(live=True, save_error=SafeRefuseError(
        "refused", capability="BOARD_MODIFY", remedy="open KiCad",
        paths_tried=["ipc", "bridge"]))
    fn = _save_project_fn(backend, tmp_path)
    pro, _ = _project_with_board(tmp_path)
    out = json.loads(fn(str(pro)))
    assert out["status"] == "info"
    assert "diverge" in out["message"]


def test_save_project_wrong_board_surfaces_error(tmp_path):
    backend = _backend(live=True,
                       save_error=ValueError("does not match open board"))
    fn = _save_project_fn(backend, tmp_path)
    pro, _ = _project_with_board(tmp_path)
    out = json.loads(fn(str(pro)))
    assert out["status"] == "error"
    assert "does not match open board" in out["message"]
