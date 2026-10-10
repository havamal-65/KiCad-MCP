"""F-DV5 (spec-courtyard-exact-overlap) — exact shape-aware courtyard overlap.

The gate keeps its overall-bbox interval test as a prefilter, then confirms
candidate pairs with exact primitive tests (circle-circle / circle-box /
box-box). Circle courtyards stop false-positive-ing from bbox squaring: the
checked-in ``ecc83_c2_u1_phantom`` fixture carries the real C2/U1 geometry
from the ecc83 recreation board, which kicad-cli DRC passes with zero
courtyard errors while the pre-fix gate flagged a 0.035 mm bbox phantom.
Pure text fixtures; no KiCad installation required.
"""

from __future__ import annotations

from pathlib import Path

from kicad_mcp.tools.drc import run_check_courtyard_overlaps
from kicad_mcp.utils.courtyard_geometry import courtyard_points, courtyard_primitives

_FIXTURES = Path(__file__).parent / "fixtures" / "boards"
_PHANTOM_BOARD = _FIXTURES / "ecc83_c2_u1_phantom.kicad_pcb"

_STROKE = '(stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd")'


def _board(*footprints: str) -> str:
    return (
        "(kicad_pcb\n"
        "\t(version 20241229)\n"
        '\t(generator "pcbnew")\n'
        '\t(net 0 "")\n'
        + "".join(footprints)
        + ")\n"
    )


def _fp(ref: str, x: float, y: float, graphics: str, rot: float = 0.0) -> str:
    at = f"(at {x} {y} {rot})" if rot else f"(at {x} {y})"
    return (
        f'\t(footprint "Test:{ref}" (layer "F.Cu") {at}\n'
        f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f"{graphics}"
        "\t)\n"
    )


def _circle_fp(ref: str, x: float, y: float, r: float, rot: float = 0.0) -> str:
    return _fp(
        ref, x, y,
        f"\t\t(fp_circle (center 0 0) (end {r} 0) {_STROKE})\n",
        rot,
    )


def _rect_fp(ref: str, x: float, y: float, half: float) -> str:
    return _fp(
        ref, x, y,
        f"\t\t(fp_rect (start -{half} -{half}) (end {half} {half}) {_STROKE})\n",
    )


def _run(tmp_path: Path, board: str) -> dict:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(board, encoding="utf-8")
    return run_check_courtyard_overlaps(p)


# ---------------------------------------------------------------------------
# X1 — primitives harvester
# ---------------------------------------------------------------------------

def test_primitives_split_circles_from_rest() -> None:
    """Mixed courtyard → circles as (cx, cy, r), rest excludes circle corners;
    courtyard_points output is unchanged by the split."""
    block = (
        '(footprint "Test:FP"\n'
        '\t(layer "F.Cu")\n'
        "\t(at 10 10)\n"
        f"\t(fp_circle (center 1 2) (end 4 2) {_STROKE})\n"
        f"\t(fp_rect (start -3 -1) (end 3 1) {_STROKE})\n"
        ")\n"
    )
    circles, rest = courtyard_primitives(block)
    assert circles == [(1.0, 2.0, 3.0)]
    assert sorted(rest) == [(-3.0, -1.0), (3.0, 1.0)]
    # courtyard_points still reports the circle as its bbox corners
    assert sorted(courtyard_points(block)) == [
        (-3.0, -1.0), (-2.0, -1.0), (3.0, 1.0), (4.0, 5.0),
    ]


def test_circle_only_courtyard_has_none_rest() -> None:
    """U1-style circle-only courtyard → rest is None in the gate's primitives,
    and both circles are harvested."""
    content = _PHANTOM_BOARD.read_text(encoding="utf-8")
    from kicad_mcp.tools.drc import _parse_courtyards_with_primitives

    _courtyards, no_courtyard, prims = _parse_courtyards_with_primitives(content)
    assert no_courtyard == []
    assert prims["U1"].rest is None
    # one F.CrtYd circle (r 10.6); the nearby r≈10.24 circle is F.SilkS and
    # must not be harvested.
    assert len(prims["U1"].circles) == 1
    assert abs(prims["U1"].circles[0][2] - 10.6) < 1e-9
    assert prims["C2"].circles == []
    assert prims["C2"].rest is not None


# ---------------------------------------------------------------------------
# X3 — the F-DV5 regression and the exact pair tests
# ---------------------------------------------------------------------------

def test_phantom_circle_box_cleared_ecc83_fixture() -> None:
    """The real C2/U1 pair (bbox phantom 0.035 mm, true clearance 1.233 mm)
    passes, matching kicad-cli DRC's 0 courtyard errors on the same file."""
    result = run_check_courtyard_overlaps(_PHANTOM_BOARD)
    assert result["passed"] is True
    assert result["footprints_checked"] == 2
    assert result["overlaps"] == []
    assert result["no_courtyard"] == []


def test_circle_box_true_overlap_detected(tmp_path: Path) -> None:
    """A box actually inside the circle's reach is still flagged."""
    # circle r=10 at x=0; box half=1.5 centered at x=10.5 → closest point
    # x=9.0, dist 9.0 < 10 → true overlap.
    result = _run(tmp_path, _board(
        _circle_fp("U1", 0, 0, 10),
        _rect_fp("C2", 10.5, 0, 1.5),
    ))
    assert result["passed"] is False
    assert result["overlap_count"] == 1
    pair = result["overlaps"][0]
    assert {pair["ref_a"], pair["ref_b"]} == {"C2", "U1"}
    assert pair["overlap_x_mm"] > 0
    assert pair["suggested_move_mm"] > 0


def test_circle_circle_phantom_cleared(tmp_path: Path) -> None:
    """Two circles whose bboxes overlap corner-to-corner but whose centers are
    farther apart than r1+r2 → passed."""
    # r=5 each at (0,0) and (7.5,7.5): bbox overlap 2.5x2.5, but dist
    # 10.61 > 10 → no true overlap.
    result = _run(tmp_path, _board(
        _circle_fp("C1", 0, 0, 5),
        _circle_fp("C2", 7.5, 7.5, 5),
    ))
    assert result["passed"] is True
    assert result["overlaps"] == []


def test_circle_circle_true_overlap_detected(tmp_path: Path) -> None:
    """dist < r1+r2 → flagged (the RW-S3 probe fixture re-covers this through
    the existing suite's DRC-oracle parity test)."""
    result = _run(tmp_path, _board(
        _circle_fp("C1", 0, 0, 5),
        _circle_fp("C2", 6, 0, 5),
    ))
    assert result["passed"] is False
    assert result["overlap_count"] == 1


def test_box_box_behavior_unchanged(tmp_path: Path) -> None:
    """Rect-courtyard pairs keep today's exact interval semantics and payload."""
    overlapping = _run(tmp_path, _board(
        _rect_fp("R1", 0, 0, 2),
        _rect_fp("R2", 3, 0, 2),
    ))
    assert overlapping["passed"] is False
    pair = overlapping["overlaps"][0]
    assert pair["overlap_x_mm"] == 1.0
    assert pair["overlap_y_mm"] == 4.0

    separated = _run(tmp_path, _board(
        _rect_fp("R1", 0, 0, 2),
        _rect_fp("R2", 5, 0, 2),
    ))
    assert separated["passed"] is True


def test_tangent_pairs_not_flagged(tmp_path: Path) -> None:
    """Touching is not overlap: diagonal tangencies whose BBOXES do overlap
    (so the pair reaches the exact test) still pass — strict semantics,
    matching the > 0 interval test."""
    # centers (0,0) and (6,8): dist = 10 = r1+r2; bboxes overlap 4x2.
    tangent_circles = _run(tmp_path, _board(
        _circle_fp("C1", 0, 0, 5),
        _circle_fp("C2", 6, 8, 5),
    ))
    assert tangent_circles["passed"] is True

    # box (3,4)-(7,8): nearest corner (3,4) at exactly dist 5 = r; bboxes
    # overlap 2x1.
    tangent_box = _run(tmp_path, _board(
        _circle_fp("C1", 0, 0, 5),
        _rect_fp("R1", 5, 6, 2),
    ))
    assert tangent_box["passed"] is True


def test_rotated_circle_bbox_from_center(tmp_path: Path) -> None:
    """A circle courtyard offset from the footprint origin on a 45°-rotated
    footprint: the overall bbox comes from the rotated CENTER ± r (pre-F-DV5
    the rotated corner points understated it), and the exact test agrees with
    hand geometry."""
    # circle center local (10, 0), r=2; footprint at (0, 0) rot 45°.
    # KiCad convention: (x,y) -> (x·c + y·s, -x·s + y·c) → center lands at
    # (7.0711, -7.0711).
    fp = _fp(
        "U1", 0, 0,
        f"\t\t(fp_circle (center 10 0) (end 12 0) {_STROKE})\n",
        rot=45,
    )
    # Probe box (8.3,-10.3)-(10.3,-8.3): nearest corner (8.3,-8.3) is
    # 1.738 mm from the ROTATED center (< r=2 → overlap) but 8.3 mm from the
    # unrotated local center (10, 0) — flags only if the center rotated.
    probe = _rect_fp("R1", 9.3, -9.3, 1.0)
    result = _run(tmp_path, _board(fp, probe))
    assert result["passed"] is False

    from kicad_mcp.tools.drc import _parse_courtyards_with_primitives

    p = tmp_path / "b.kicad_pcb"
    courtyards, _no, prims = _parse_courtyards_with_primitives(
        p.read_text(encoding="utf-8")
    )
    cx, cy, r = prims["U1"].circles[0]
    assert abs(cx - 7.0711) < 1e-3
    assert abs(cy + 7.0711) < 1e-3
    assert abs(r - 2.0) < 1e-9
    box = courtyards["U1"]
    assert abs(box["xmin"] - (cx - 2)) < 1e-9
    assert abs(box["xmax"] - (cx + 2)) < 1e-9
    assert abs(box["ymin"] - (cy - 2)) < 1e-9
    assert abs(box["ymax"] - (cy + 2)) < 1e-9
