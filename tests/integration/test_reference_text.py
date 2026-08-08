"""Integration tests for the FD (KIR3 S4) reference-text bridge handlers.

Exercises ``get_reference_text`` / ``get_reference_texts`` / ``set_reference_text``
against the live pcbnew bridge — the class of coverage unit tests can't give
(real PCB_FIELD/FP_TEXT objects, real save round-trips, wx-thread dispatch).

Non-destructive by construction: every mutation is restored in ``finally`` so
the shared scratch board's reference fields end the session where they started
(the session byte-snapshot guard backstops any failure mid-restore).
"""

from __future__ import annotations

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call

pytestmark = pytest.mark.integration

_TOL = 1e-3  # mm — nm-integer round-trip tolerance

_RECORD_KEYS = {
    "reference", "visible", "layer", "ref_x", "ref_y", "ref_w", "ref_h",
    "fp_x0", "fp_y0", "fp_x1", "fp_y1",
}


def _board_path(bridge_session) -> str:
    path = bridge_session.get("board_path")
    if not path:
        pytest.skip("Bridge has no open board (board_path empty in ping).")
    return path


def _first_record(path: str) -> dict:
    result = _tcp_call("get_reference_texts", timeout=15.0, path=path)
    refs = result.get("references", [])
    if not refs:
        pytest.skip("Open board has no footprints to exercise.")
    return refs[0]


def test_bulk_read_shape_and_geometry(bridge_session):
    path = _board_path(bridge_session)
    result = _tcp_call("get_reference_texts", timeout=15.0, path=path)
    refs = result.get("references", [])
    assert refs, "expected at least one footprint record"
    for rec in refs:
        assert _RECORD_KEYS <= set(rec), f"missing keys in {rec.get('reference')}"
        # Footprint body box must be well-formed; text box must have real size.
        assert rec["fp_x1"] > rec["fp_x0"] and rec["fp_y1"] > rec["fp_y0"]
        assert rec["ref_w"] > 0 and rec["ref_h"] > 0


def test_single_read_matches_bulk(bridge_session):
    path = _board_path(bridge_session)
    rec = _first_record(path)
    single = _tcp_call("get_reference_text", timeout=15.0,
                       path=path, reference=rec["reference"])
    assert single["reference"] == rec["reference"]
    assert abs(single["ref_x"] - rec["ref_x"]) < _TOL
    assert abs(single["ref_y"] - rec["ref_y"]) < _TOL
    assert single["visible"] == rec["visible"]


def test_absolute_move_round_trips_and_restores(bridge_session):
    path = _board_path(bridge_session)
    rec = _first_record(path)
    ref = rec["reference"]
    ox, oy = rec["ref_x"], rec["ref_y"]
    tx, ty = ox + 3.5, oy - 2.25
    try:
        result = _tcp_call("set_reference_text", timeout=15.0,
                           path=path, reference=ref, x=tx, y=ty)
        assert abs(result["after"]["x"] - tx) < _TOL
        assert abs(result["after"]["y"] - ty) < _TOL
        assert abs(result["before"]["x"] - ox) < _TOL
        # Re-read through the bulk handler — the move must be observable.
        moved = next(r for r in _tcp_call("get_reference_texts", timeout=15.0,
                                          path=path)["references"]
                     if r["reference"] == ref)
        assert abs(moved["ref_x"] - tx) < _TOL and abs(moved["ref_y"] - ty) < _TOL
    finally:
        _tcp_call("set_reference_text", timeout=15.0,
                  path=path, reference=ref, x=ox, y=oy)
    restored = _tcp_call("get_reference_text", timeout=15.0,
                         path=path, reference=ref)
    assert abs(restored["ref_x"] - ox) < _TOL and abs(restored["ref_y"] - oy) < _TOL


def test_relative_move_applies_delta(bridge_session):
    path = _board_path(bridge_session)
    rec = _first_record(path)
    ref = rec["reference"]
    ox, oy = rec["ref_x"], rec["ref_y"]
    try:
        result = _tcp_call("set_reference_text", timeout=15.0,
                           path=path, reference=ref, dx=1.0, dy=-0.5)
        assert abs(result["after"]["x"] - (ox + 1.0)) < _TOL
        assert abs(result["after"]["y"] - (oy - 0.5)) < _TOL
    finally:
        _tcp_call("set_reference_text", timeout=15.0,
                  path=path, reference=ref, x=ox, y=oy)


def test_visibility_toggle_and_restore(bridge_session):
    path = _board_path(bridge_session)
    rec = _first_record(path)
    ref = rec["reference"]
    orig = rec["visible"]
    try:
        result = _tcp_call("set_reference_text", timeout=15.0,
                           path=path, reference=ref, visible=not orig)
        assert result["after"]["visible"] == (not orig)
        seen = _tcp_call("get_reference_text", timeout=15.0,
                         path=path, reference=ref)
        assert seen["visible"] == (not orig)
    finally:
        _tcp_call("set_reference_text", timeout=15.0,
                  path=path, reference=ref, visible=orig)
    assert _tcp_call("get_reference_text", timeout=15.0,
                     path=path, reference=ref)["visible"] == orig


def test_unknown_reference_is_a_clean_error(bridge_session):
    path = _board_path(bridge_session)
    with pytest.raises(Exception, match="not found"):
        _tcp_call("get_reference_text", timeout=15.0,
                  path=path, reference="T17_NO_SUCH_REF")
    with pytest.raises(Exception, match="not found"):
        _tcp_call("set_reference_text", timeout=15.0,
                  path=path, reference="T17_NO_SUCH_REF", visible=False)
