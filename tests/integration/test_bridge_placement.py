"""REQ-COV-014/015 — auto_place anchors and add_board_outline.

auto_place must leave anchored refs immobile while still placing the rest;
the spec note (IQ-3) records why we deliberately don't assert "non-anchored
moved" — the row layout can re-emit the same x within rounding.

add_board_outline removes every existing Edge.Cuts item before adding the
new one — this test mutates the developer's board outline. Run it on a
throwaway board.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration

_RESISTOR_FP = "Resistor_SMD:R_0805_2012Metric"
_TOL_MM = 0.001  # 1 µm


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def _find_component(components: list[dict], reference: str) -> dict | None:
    for comp in components:
        if comp.get("reference") == reference:
            return comp
    return None


def test_auto_place_anchors_immobile(bridge_session):
    """REQ-COV-014: anchored refs in auto_place keep their positions; others get placed."""
    path = _board_path()
    refs = ["T14_R1", "T14_R2", "T14_R3", "T14_R4"]
    initial_positions = [(10.0, 10.0), (15.0, 10.0), (20.0, 10.0), (25.0, 10.0)]

    # Setup on a shared scratch fixture (#16/REQ-FIX-1): the T14_* refs are
    # canonical fixture content — MOVE existing ones to the wanted pose and
    # only place (then remove) the missing ones; every ref this test touched
    # is restored/removed in the finally.
    components = _tcp_call("get_components", 5.0, path=path)
    existing = {c["reference"]: c for c in components if c.get("reference") in refs}
    pose_snapshot = {
        r: (c["x"], c["y"], c.get("rotation", 0.0)) for r, c in existing.items()
    }
    placed_by_test: list[str] = []
    for r, (x, y) in zip(refs, initial_positions):
        if r in existing:
            _tcp_call("move_component", 5.0, path=path, reference=r,
                      x=x, y=y, rotation=0)
        else:
            _tcp_call(
                "place_component", 5.0,
                path=path, reference=r, footprint=_RESISTOR_FP,
                x=x, y=y, rotation=0,
            )
            placed_by_test.append(r)

    try:
        auto_result = _tcp_call(
            "auto_place", 10.0,
            path=path,
            board_x=0.0, board_y=0.0,
            board_width=80.0, board_height=80.0,
            anchors=["T14_R1", "T14_R2"],
        )
        assert isinstance(auto_result, dict), f"unexpected auto_place response: {auto_result!r}"
        placed = auto_result.get("placed", [])
        warnings = auto_result.get("warnings", [])
        assert "T14_R3" in placed, f"T14_R3 missing from placed: placed={placed!r}, warnings={warnings!r}"
        assert "T14_R4" in placed, f"T14_R4 missing from placed: placed={placed!r}, warnings={warnings!r}"
        # Anchored refs must NOT appear in placed (they were skipped).
        assert "T14_R1" not in placed, f"anchored T14_R1 was placed: {placed!r}"
        assert "T14_R2" not in placed, f"anchored T14_R2 was placed: {placed!r}"

        # Final positions: anchors unchanged.
        components = _tcp_call("get_components", 5.0, path=path)
        for ref, (ix, iy) in zip(refs[:2], initial_positions[:2]):
            comp = _find_component(components, ref)
            assert comp is not None, f"anchored {ref} disappeared"
            assert abs(comp["x"] - ix) < _TOL_MM, (
                f"anchored {ref} moved from x={ix} to x={comp['x']} — auto_place must NOT move anchors"
            )
            assert abs(comp["y"] - iy) < _TOL_MM, (
                f"anchored {ref} moved from y={iy} to y={comp['y']} — auto_place must NOT move anchors"
            )
    finally:
        for r in placed_by_test:
            try:
                _tcp_call("remove_component", 10.0, path=path, reference=r)
            except RuntimeError:
                pass
        for r, (ox, oy, orot) in pose_snapshot.items():
            _tcp_call("move_component", 5.0, path=path, reference=r,
                      x=ox, y=oy, rotation=orot)


def test_add_board_outline_creates_edge_cuts_polygon(bridge_session):
    """REQ-COV-015: add_board_outline emits exactly one (gr_rect ...) on Edge.Cuts.

    Mutates the developer's board outline — the handler removes every existing
    Edge.Cuts item before adding the new one. Run on a throwaway board.
    """
    path = _board_path()
    _tcp_call(
        "add_board_outline", 5.0,
        path=path, x=0.0, y=0.0, width=40.0, height=30.0,
    )
    text = Path(path).read_text(encoding="utf-8")

    # Find every (gr_rect ...) on Edge.Cuts. Look for the start/end pair
    # and the layer marker inside one balanced block.
    edge_rects: list[tuple[float, float, float, float]] = []
    for match in re.finditer(r"\(gr_rect\s+", text):
        start = match.start()
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    break
            i += 1
        else:
            continue
        if '"Edge.Cuts"' not in block:
            continue
        start_match = re.search(r"\(start\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)", block)
        end_match = re.search(r"\(end\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)", block)
        if not start_match or not end_match:
            continue
        edge_rects.append((
            float(start_match.group(1)), float(start_match.group(2)),
            float(end_match.group(1)), float(end_match.group(2)),
        ))

    assert len(edge_rects) == 1, (
        f"expected exactly 1 Edge.Cuts gr_rect after add_board_outline "
        f"(handler removes existing); got {len(edge_rects)}: {edge_rects!r}"
    )
    sx, sy, ex, ey = edge_rects[0]
    assert abs(sx - 0.0) < _TOL_MM and abs(sy - 0.0) < _TOL_MM, edge_rects[0]
    assert abs(ex - 40.0) < _TOL_MM and abs(ey - 30.0) < _TOL_MM, edge_rects[0]
