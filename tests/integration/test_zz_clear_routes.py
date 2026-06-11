"""REQ-COV-013 — clear_routes empties all tracks and vias.

Destructive: removes every track and via on the shared board. Filename
prefix `test_zz_` ensures alphabetical collection puts this last so
earlier tests' routing work is not poisoned (REQ-ISO-005).

Anchors the bridge-cache-desync fix: pre-fix, clear_routes wrote to
disk but the bridge held its own track list, so subsequent get_tracks
calls returned a stale set. The handler now mutates the live
in-memory board.
"""

from __future__ import annotations

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def test_clear_routes_empties_tracks_and_vias(bridge_session):
    """REQ-COV-013: clear_routes leaves get_tracks empty after adding tracks/vias.

    Adds 2 tracks and 1 via, calls clear_routes(backup=False), then asserts
    get_tracks is empty. The assertion is on the full board (not just this
    test's additions) — clear_routes promises to remove ALL routing.
    """
    path = _board_path()

    # Self-setup: add some routing so the clear has something to clear.
    _tcp_call(
        "add_track", 5.0,
        path=path, start_x=200.0, start_y=200.0,
        end_x=210.0, end_y=200.0, width=0.2, layer="F.Cu",
    )
    _tcp_call(
        "add_track", 5.0,
        path=path, start_x=200.0, start_y=205.0,
        end_x=210.0, end_y=205.0, width=0.2, layer="F.Cu",
    )
    _tcp_call(
        "add_via", 5.0,
        path=path, x=215.0, y=200.0, drill=0.4, size=0.8,
    )
    # Sanity: there is now at least 1 track and 1 via on the board.
    tracks_before = _tcp_call("get_tracks", 5.0, path=path)
    assert any(t.get("type") == "track" for t in tracks_before), (
        f"setup failed — no tracks visible to get_tracks after add_track: {tracks_before!r}"
    )
    assert any(t.get("type") == "via" for t in tracks_before), (
        f"setup failed — no vias visible to get_tracks after add_via: {tracks_before!r}"
    )

    # backup=False: do NOT leave a .clear_routes_backup.kicad_pcb file in
    # the developer's project folder every test run.
    result = _tcp_call("clear_routes", 5.0, path=path, backup=False)
    assert isinstance(result, dict), f"unexpected clear_routes response: {result!r}"
    assert result.get("status") == "success", result

    tracks_after = _tcp_call("get_tracks", 5.0, path=path)
    assert tracks_after == [], (
        f"clear_routes did not empty the track list — bridge-cache-desync regression. "
        f"remaining: {tracks_after!r}"
    )
