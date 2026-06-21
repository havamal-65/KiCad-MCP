"""Tests for #14C save_project coherence messaging.

save_project's response now states which backend persisted the file and whether
a live KiCad/pcbnew bridge session is open (so its in-memory board can diverge
from disk). We register the project tools against a capturing MCP stub and a
mocked backend, then invoke the captured save_project closure directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kicad_mcp.backends.base import BackendCapability
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


def _backend(*, live: bool):
    backend = MagicMock()
    backend.has_capability.return_value = False  # file-based (no REAL_TIME_SYNC)
    if live:
        backend.get_active_project.return_value = {"project_name": "demo"}
    else:
        backend.get_active_project.side_effect = RuntimeError("no live session")
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


def test_save_project_ipc_backend_reports_success(tmp_path):
    backend = MagicMock()
    backend.has_capability.return_value = True  # REAL_TIME_SYNC capable
    backend.get_active_project.return_value = {"project_name": "demo"}
    fn = _save_project_fn(backend, tmp_path)
    out = json.loads(fn("/tmp/demo.kicad_pro"))
    assert out["status"] == "success"
    assert out["live_bridge_session"] is True
    backend.has_capability.assert_called_with(BackendCapability.REAL_TIME_SYNC)
