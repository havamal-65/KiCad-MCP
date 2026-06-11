"""REQ-COV-003/004/005 — basic read handlers.

Three read-only handlers that every later test depends on. If these fail,
the bridge is wired up but the read surface is broken; everything else
becomes noise. They run alphabetically before all the placement/routing
tests so failures here surface first.
"""

from __future__ import annotations

import os

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration


def _board_path(bridge_session) -> str:
    """Resolve the open board's path via get_active_project."""
    result = _tcp_call("get_active_project", 5.0)
    path = result["board_path"]
    assert path, "get_active_project returned empty board_path; ensure pcbnew has a board open"
    return path


def test_get_active_project_returns_open_board(bridge_session):
    """REQ-COV-003: get_active_project names the board pcbnew has open."""
    result = _tcp_call("get_active_project", 5.0)
    assert isinstance(result, dict)
    assert "board_path" in result
    board_path = result["board_path"]
    assert board_path, f"board_path was empty: {result!r}"
    # Must be an existing file on disk.
    norm = os.path.normpath(os.path.normcase(board_path))
    assert os.path.isfile(norm), f"board_path does not point to a real file: {norm}"


def test_get_board_info_payload_shape(bridge_session):
    """REQ-COV-004: get_board_info returns the documented fields with sane types."""
    path = _board_path(bridge_session)
    info = _tcp_call("get_board_info", 5.0, path=path)

    assert isinstance(info, dict)
    for key in ("title", "revision", "layer_count", "width_mm", "height_mm",
                "net_count", "footprint_count"):
        assert key in info, f"missing key {key!r} in get_board_info payload: {info!r}"
    assert isinstance(info["layer_count"], int) and info["layer_count"] >= 2, info
    assert isinstance(info["width_mm"], (int, float)), info
    assert isinstance(info["height_mm"], (int, float)), info


def test_get_components_returns_list(bridge_session):
    """REQ-COV-005: get_components returns list[dict] with expected per-component keys."""
    path = _board_path(bridge_session)
    components = _tcp_call("get_components", 5.0, path=path)

    assert isinstance(components, list)
    for comp in components:
        assert isinstance(comp, dict), f"non-dict component entry: {comp!r}"
        for key in ("reference", "x", "y", "layer"):
            assert key in comp, f"missing key {key!r} in component: {comp!r}"
