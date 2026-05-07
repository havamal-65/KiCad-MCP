"""Tests for the bridge-side clear_routes path.

Covers:
- PluginBoardOps.clear_routes — TCP serialization layer (mocks _tcp_call)
- _impl_clear_routes — tool wrapper bridge-first dispatch + file fallback

The pcbnew-side _handle_clear_routes implementation lives inside KiCad's
embedded Python and is verified in live E2E (Codex), not here.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
)
from kicad_mcp.tools.routing import _impl_clear_routes
from kicad_mcp.utils.change_log import ChangeLog


# ---------------------------------------------------------------------------
# PluginBoardOps.clear_routes — bridge TCP layer
# ---------------------------------------------------------------------------

def _patch_tcp(returned: dict | None = None, raises: Exception | None = None):
    """Patch _tcp_call to either return a dict or raise."""
    captured: dict = {}

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        captured["method"] = method
        captured["timeout"] = timeout
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return returned or {}

    return patch("kicad_mcp.backends.plugin_backend._tcp_call",
                 side_effect=fake_tcp_call), captured


def test_plugin_board_ops_dispatches_clear_routes_method():
    bridge_response = {
        "status": "success",
        "tracks_removed": 12,
        "vias_removed": 3,
        "backup_path": "/tmp/board.clear_routes_backup.kicad_pcb",
    }
    patcher, captured = _patch_tcp(returned=bridge_response)
    with patcher:
        result = PluginBoardOps().clear_routes(Path("/tmp/board.kicad_pcb"), backup=True)

    assert captured["method"] == "clear_routes"
    assert captured["kwargs"]["path"] == str(Path("/tmp/board.kicad_pcb"))
    assert captured["kwargs"]["backup"] is True
    assert result == bridge_response


def test_plugin_board_ops_passes_backup_false():
    patcher, captured = _patch_tcp(returned={"status": "success"})
    with patcher:
        PluginBoardOps().clear_routes(Path("/tmp/b.kicad_pcb"), backup=False)
    assert captured["kwargs"]["backup"] is False


def test_plugin_board_ops_propagates_bridge_unavailable():
    patcher, _ = _patch_tcp(
        raises=BridgeTemporarilyUnavailableError("bridge down"),
    )
    with patcher, pytest.raises(BridgeTemporarilyUnavailableError):
        PluginBoardOps().clear_routes(Path("/tmp/b.kicad_pcb"))


# ---------------------------------------------------------------------------
# _impl_clear_routes — tool wrapper bridge-first dispatch + fallback
# ---------------------------------------------------------------------------

BOARD_WITH_ROUTES = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "GND")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
      )
      (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1))
      (via (at 15 15) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
    )
""")


@pytest.fixture
def board_file(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_WITH_ROUTES, encoding="utf-8")
    return f


@pytest.fixture
def change_log(tmp_path: Path) -> ChangeLog:
    return ChangeLog(tmp_path / "changes.json")


def _make_backend_returning(result: dict) -> MagicMock:
    """Build a backend mock whose get_board_modify_ops().clear_routes returns result."""
    ops = MagicMock()
    ops.clear_routes.return_value = result
    backend = MagicMock()
    backend.get_board_modify_ops.return_value = ops
    return backend


def _make_backend_raising(exc: Exception) -> MagicMock:
    ops = MagicMock()
    ops.clear_routes.side_effect = exc
    backend = MagicMock()
    backend.get_board_modify_ops.return_value = ops
    return backend


def test_impl_uses_bridge_when_available(board_file: Path, change_log: ChangeLog):
    bridge_result = {
        "status": "success",
        "tracks_removed": 5,
        "vias_removed": 1,
        "backup_path": None,
    }
    backend = _make_backend_returning(bridge_result)

    out = json.loads(_impl_clear_routes(
        str(board_file), backup=False, backend=backend, change_log=change_log,
    ))

    assert out == bridge_result
    backend.get_board_modify_ops.assert_called_once()
    ops = backend.get_board_modify_ops.return_value
    args, kwargs = ops.clear_routes.call_args
    assert kwargs.get("backup") is False
    # Path argument is normalized via validate_kicad_path; it should be a Path
    assert Path(args[0]) == board_file


def test_impl_falls_back_to_file_when_bridge_unavailable(
    board_file: Path, change_log: ChangeLog,
):
    backend = _make_backend_raising(
        BridgeTemporarilyUnavailableError("bridge down"),
    )

    out = json.loads(_impl_clear_routes(
        str(board_file), backup=False, backend=backend, change_log=change_log,
    ))

    assert out["status"] == "success"
    assert out["tracks_removed"] == 1
    assert out["vias_removed"] == 1

    # File backend mutated the on-disk file
    content = board_file.read_text(encoding="utf-8")
    assert "(segment" not in content
    assert "(via" not in content
    # Footprint preserved
    assert "(footprint" in content


def test_impl_falls_back_when_get_board_modify_ops_raises(
    board_file: Path, change_log: ChangeLog,
):
    """Real prod path: _check_bridge() raises from get_board_modify_ops(), not
    from the ops.clear_routes() call itself. Ensures both raise sites trigger
    the fallback."""
    backend = MagicMock()
    backend.get_board_modify_ops.side_effect = BridgeTemporarilyUnavailableError(
        "bridge down at accessor",
    )

    out = json.loads(_impl_clear_routes(
        str(board_file), backup=False, backend=backend, change_log=change_log,
    ))

    assert out["status"] == "success"
    assert out["tracks_removed"] == 1
    assert out["vias_removed"] == 1
    content = board_file.read_text(encoding="utf-8")
    assert "(segment" not in content


def test_impl_records_change_log(board_file: Path, change_log: ChangeLog):
    backend = _make_backend_returning({
        "status": "success", "tracks_removed": 7, "vias_removed": 2, "backup_path": None,
    })

    _impl_clear_routes(
        str(board_file), backup=False, backend=backend, change_log=change_log,
    )

    entries = change_log.get_recent()
    assert len(entries) == 1
    e = entries[0]
    assert e["tool"] == "clear_routes"
    assert e["params"]["tracks_removed"] == 7
    assert e["params"]["vias_removed"] == 2
