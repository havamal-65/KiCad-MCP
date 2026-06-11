"""REQ-COV-011/012 — track and via routing handlers.

add_track is the most basic routing primitive — every autoroute output
gets reified through this code path. add_via anchors the `SetDrill`
rename fix: pcbnew's via API uses SetDrill (not SetDrillSize), and the
bridge's handler must use the right method or vias come out with the
default drill value.
"""

from __future__ import annotations

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration

_TOL_MM = 0.001  # 1 µm


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def _find_track(tracks: list[dict], start_xy: tuple[float, float],
                end_xy: tuple[float, float]) -> dict | None:
    for t in tracks:
        if t.get("type") != "track":
            continue
        if (abs(t["start_x"] - start_xy[0]) < _TOL_MM
                and abs(t["start_y"] - start_xy[1]) < _TOL_MM
                and abs(t["end_x"] - end_xy[0]) < _TOL_MM
                and abs(t["end_y"] - end_xy[1]) < _TOL_MM):
            return t
        # Track endpoints may be returned in reverse order.
        if (abs(t["start_x"] - end_xy[0]) < _TOL_MM
                and abs(t["start_y"] - end_xy[1]) < _TOL_MM
                and abs(t["end_x"] - start_xy[0]) < _TOL_MM
                and abs(t["end_y"] - start_xy[1]) < _TOL_MM):
            return t
    return None


def _find_via(tracks: list[dict], xy: tuple[float, float]) -> dict | None:
    for t in tracks:
        if t.get("type") != "via":
            continue
        if abs(t["x"] - xy[0]) < _TOL_MM and abs(t["y"] - xy[1]) < _TOL_MM:
            return t
    return None


def test_add_track_creates_segment_on_fcu(bridge_session):
    """REQ-COV-011: add_track produces a track segment with matching endpoints/width/layer."""
    path = _board_path()
    start = (5.0, 5.0)
    end = (15.0, 5.0)
    _tcp_call(
        "add_track", 5.0,
        path=path, start_x=start[0], start_y=start[1],
        end_x=end[0], end_y=end[1], width=0.2, layer="F.Cu",
    )
    tracks = _tcp_call("get_tracks", 5.0, path=path)
    track = _find_track(tracks, start, end)
    assert track is not None, (
        f"no track with endpoints {start}-{end} in get_tracks: "
        f"{[t for t in tracks if t.get('type') == 'track']}"
    )
    assert abs(track["width"] - 0.2) < _TOL_MM, track
    assert track["layer"] == "F.Cu", track


def test_add_via_drill_size_persisted(bridge_session):
    """REQ-COV-012: add_via writes the requested drill size — anchors SetDrill fix."""
    path = _board_path()
    pos = (20.0, 20.0)
    _tcp_call(
        "add_via", 5.0,
        path=path, x=pos[0], y=pos[1], drill=0.5, size=0.8,
    )
    tracks = _tcp_call("get_tracks", 5.0, path=path)
    via = _find_via(tracks, pos)
    assert via is not None, (
        f"no via at {pos} in get_tracks: "
        f"{[t for t in tracks if t.get('type') == 'via']}"
    )
    assert abs(via["drill"] - 0.5) < _TOL_MM, (
        f"via drill mismatch — expected 0.5, got {via['drill']}. "
        f"SetDrill rename regression — bridge wrote the default drill instead."
    )
    assert abs(via["size"] - 0.8) < _TOL_MM, via
