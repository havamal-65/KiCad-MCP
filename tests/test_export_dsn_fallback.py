"""Tests for #13C — boardless/unreachable bridge fallback in export_dsn.

A live bridge that is reachable but has no board ("No board is currently open",
or the detached-SwigPyObject GetFileName error) previously hard-stopped DSN
export. _export_dsn_with_fallback now degrades to a headless subprocess that
loads the board from disk, mirroring _impl_clear_routes' fallback for an
unreachable bridge.

The subprocess export itself runs pcbnew and is verified live; here we mock it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.plugin_backend import BridgeTemporarilyUnavailableError
from kicad_mcp.tools.routing import (
    _export_dsn_with_fallback,
    _is_boardless_bridge_error,
)


# ---------------------------------------------------------------------------
# _is_boardless_bridge_error — pattern matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "No board is currently open in KiCad",
    "'SwigPyObject' object has no attribute 'GetFileName'",
    "RuntimeError: SwigPyObject ... GetFileName failed",
    # Live-observed on KiCad 9 (2026-06-23): pcbnew open with no board reports an
    # empty open board, so the path-match check fails against ''.
    "Requested board 'D:/proj/test_board.kicad_pcb' does not match open board ''. "
    "Open the correct .kicad_pcb file in KiCad first.",
])
def test_boardless_patterns_match(msg):
    assert _is_boardless_bridge_error(RuntimeError(msg)) is True


@pytest.mark.parametrize("msg", [
    "ExportSpecctraDSN failed for foo.dsn",
    "some unrelated bridge error",
    # A mismatch against a DIFFERENT real board is a genuine wrong-board error —
    # must NOT silently route to disk.
    "Requested board 'D:/proj/test_board.kicad_pcb' does not match open board "
    "'D:/proj/other.kicad_pcb'. Open the correct .kicad_pcb file in KiCad first.",
])
def test_non_boardless_patterns_do_not_match(msg):
    assert _is_boardless_bridge_error(RuntimeError(msg)) is False


# ---------------------------------------------------------------------------
# _export_dsn_with_fallback — routing logic
# ---------------------------------------------------------------------------

def _backend(export_side):
    backend = MagicMock()
    if isinstance(export_side, Exception):
        backend.export_dsn.side_effect = export_side
    else:
        backend.export_dsn.return_value = export_side
    return backend


def test_uses_bridge_when_it_succeeds(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    dsn = tmp_path / "b.dsn"
    backend = _backend({"success": True, "dsn_path": str(dsn)})

    with patch("kicad_mcp.tools.routing._export_dsn_subprocess") as sub:
        result = _export_dsn_with_fallback(backend, p, dsn)

    sub.assert_not_called()
    assert result == {"success": True, "dsn_path": str(dsn)}


def test_falls_back_when_bridge_unreachable(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    dsn = tmp_path / "b.dsn"
    backend = _backend(BridgeTemporarilyUnavailableError("bridge down"))

    with patch(
        "kicad_mcp.tools.routing._export_dsn_subprocess",
        return_value={"success": True, "via": "subprocess"},
    ) as sub:
        result = _export_dsn_with_fallback(backend, p, dsn)

    sub.assert_called_once_with(p, dsn)
    assert result["via"] == "subprocess"


def test_falls_back_when_bridge_boardless(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    dsn = tmp_path / "b.dsn"
    backend = _backend(RuntimeError("Plugin bridge error: No board is currently open"))

    with patch(
        "kicad_mcp.tools.routing._export_dsn_subprocess",
        return_value={"success": True, "via": "subprocess"},
    ) as sub:
        result = _export_dsn_with_fallback(backend, p, dsn)

    sub.assert_called_once_with(p, dsn)
    assert result["via"] == "subprocess"


def test_reraises_unrelated_bridge_error(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    dsn = tmp_path / "b.dsn"
    backend = _backend(RuntimeError("ExportSpecctraDSN failed for b.dsn"))

    with patch("kicad_mcp.tools.routing._export_dsn_subprocess") as sub:
        with pytest.raises(RuntimeError, match="ExportSpecctraDSN failed"):
            _export_dsn_with_fallback(backend, p, dsn)
    sub.assert_not_called()
