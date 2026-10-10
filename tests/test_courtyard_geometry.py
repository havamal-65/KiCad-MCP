"""RW-S3 (spec-courtyard-geometry) — the shared courtyard shape harvester.

G1: ``courtyard_points`` covers every courtyard graphic (rect/line unchanged,
poly vertices, circle exact bbox, arc exact bbox via in-sweep axis extremes,
Bézier control hull). G2/G3: the gate parser and payloads see them — pinned
against the checked-in probe board + its kicad-cli DRC oracle snapshot (AC3).
Pure text fixtures; no KiCad installation required.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kicad_mcp.tools.drc import run_check_courtyard_overlaps
from kicad_mcp.utils.courtyard_geometry import courtyard_points
from kicad_mcp.utils.placement_metrics import placement_metric

_FIXTURES = Path(__file__).parent / "fixtures" / "boards"
_PROBE_BOARD = _FIXTURES / "circular_courtyard_overlap.kicad_pcb"
_PROBE_ORACLE = _FIXTURES / "circular_courtyard_overlap.drc.json"


def _fp(graphics: str) -> str:
    """A minimal footprint block wrapping the given graphic sub-blocks."""
    return (
        '(footprint "Test:FP"\n'
        '\t(layer "F.Cu")\n'
        "\t(at 10 10)\n"
        '\t(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        f"{graphics}"
        '\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        ")\n"
    )


def _approx_point_set(
    points: list[tuple[float, float]],
    expected: set[tuple[float, float]],
) -> None:
    assert len(points) == len(expected)
    rounded = {(round(x, 6), round(y, 6)) for x, y in points}
    assert rounded == expected


# ---------------------------------------------------------------------------
# G1 — shape table (spec tests 1-5)
# ---------------------------------------------------------------------------

def test_circle_courtyard_bbox_exact() -> None:
    """Valve-style fp_circle → the two corners of the exact center±r bbox."""
    block = _fp(
        "\t(fp_circle (center 0 1.6) (end 10.25 1.6) "
        '(stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd"))\n'
    )
    _approx_point_set(
        courtyard_points(block),
        {(-10.25, -8.65), (10.25, 11.85)},
    )


def test_poly_courtyard_xy_vertices() -> None:
    block = _fp(
        "\t(fp_poly (pts (xy -1 -2) (xy 4 -2) (xy 4 3) (xy 0.5 3.5)) "
        '(stroke (width 0.05) (type default)) (fill no) (layer "B.CrtYd"))\n'
    )
    _approx_point_set(
        courtyard_points(block),
        {(-1.0, -2.0), (4.0, -2.0), (4.0, 3.0), (0.5, 3.5)},
    )


def test_arc_courtyard_axis_extremes_in_sweep() -> None:
    """Arc on the r=5 circle about the origin from (-3,-4) through (5,0) to
    (-3,4): the +x, +y, and -y axis extremes lie inside the sweep, -x does
    not — the harvest is the exact arc bbox corner set."""
    block = _fp(
        "\t(fp_arc (start -3 -4) (mid 5 0) (end -3 4) "
        '(stroke (width 0.05) (type default)) (layer "F.CrtYd"))\n'
    )
    points = courtyard_points(block)
    _approx_point_set(
        points,
        {(-3.0, -4.0), (-3.0, 4.0), (5.0, 0.0), (0.0, 5.0), (0.0, -5.0)},
    )
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    assert (min(xs), min(ys), max(xs), max(ys)) == (-3.0, -5.0, 5.0, 5.0)


def test_arc_collinear_fallback() -> None:
    """A degenerate (collinear) start/mid/end triple has no circumcircle —
    the three raw points come back unchanged."""
    block = _fp(
        "\t(fp_arc (start 0 0) (mid 1 0) (end 2 0) "
        '(stroke (width 0.05) (type default)) (layer "F.CrtYd"))\n'
    )
    _approx_point_set(
        courtyard_points(block),
        {(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)},
    )


def test_curve_control_hull_conservative() -> None:
    """A Bézier contributes its four control points — a conservative superset
    (the curve lies within its control hull)."""
    block = _fp(
        "\t(fp_curve (pts (xy 0 0) (xy 1 5) (xy 3 5) (xy 4 0)) "
        '(stroke (width 0.05) (type default)) (layer "F.CrtYd"))\n'
    )
    points = courtyard_points(block)
    _approx_point_set(
        points,
        {(0.0, 0.0), (1.0, 5.0), (3.0, 5.0), (4.0, 0.0)},
    )
    # The cubic's true y-extreme is 3/4 of the control height — the hull bbox
    # bounds it from above (conservative, never under-reports).
    assert max(p[1] for p in points) >= 0.75 * 5.0


def test_rect_line_behavior_unchanged() -> None:
    """fp_rect/fp_line harvest exactly the (start …)/(end …) pairs the
    pre-RW-S3 parsers read — byte-parity — and non-courtyard layers stay
    invisible."""
    graphics = (
        "\t(fp_rect (start -2 -3) (end 2 3) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        "\t(fp_line (start -2 3) (end 2 3) "
        '(stroke (width 0.05) (type solid)) (layer "B.CrtYd"))\n'
        "\t(fp_line (start -99 -99) (end 99 99) "
        '(stroke (width 0.05) (type solid)) (layer "F.SilkS"))\n'
    )
    block = _fp(graphics)
    # The old parsers harvested every (start …)/(end …) inside courtyard
    # rect/line sub-blocks in document order — exactly this literal.
    assert courtyard_points(block) == [
        (-2.0, -3.0), (2.0, 3.0), (-2.0, 3.0), (2.0, 3.0),
    ]


# ---------------------------------------------------------------------------
# G2/G3 — the gate sees circles; no_courtyard is never silent (tests 6, 7, 9)
# ---------------------------------------------------------------------------

def test_fixture_circular_overlap_detected() -> None:
    """AC3 — the probe board (two r=5mm circle-only courtyards 6mm apart):
    the gate now fails with pair (C1, C2), and its verdict matches the
    checked-in kicad-cli DRC oracle snapshot."""
    result = run_check_courtyard_overlaps(_PROBE_BOARD)
    assert result["passed"] is False
    assert result["footprints_checked"] == 2
    assert result["overlap_count"] == 1
    pair = {result["overlaps"][0]["ref_a"], result["overlaps"][0]["ref_b"]}
    assert pair == {"C1", "C2"}
    assert result["no_courtyard"] == []
    assert result["no_courtyard_count"] == 0

    oracle = json.loads(_PROBE_ORACLE.read_text(encoding="utf-8"))
    drc_pairs = [
        {
            m.group(1)
            for item in v["items"]
            if (m := re.match(r"Footprint (\S+)", item["description"]))
        }
        for v in oracle["violations"]
        if v["type"] == "courtyards_overlap" and v["severity"] == "error"
    ]
    assert drc_pairs == [{"C1", "C2"}]
    assert (len(drc_pairs) > 0) == (result["passed"] is False)


def test_no_courtyard_listed_in_overlap_payload(tmp_path: Path) -> None:
    """A footprint with no courtyard geometry is named in the payload instead
    of silently vanishing."""
    board = (
        "(kicad_pcb\n"
        "\t(version 20241229)\n"
        '\t(generator "pcbnew")\n'
        '\t(net 0 "")\n'
        '\t(footprint "Test:A" (layer "F.Cu") (at 10 10)\n'
        '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        "\t\t(fp_rect (start -1 -1) (end 1 1) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        "\t)\n"
        '\t(footprint "Test:B" (layer "F.Cu") (at 30 10)\n'
        '\t\t(property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))\n'
        "\t)\n"
        ")\n"
    )
    p = tmp_path / "b.kicad_pcb"
    p.write_text(board, encoding="utf-8")
    result = run_check_courtyard_overlaps(p)
    assert result["passed"] is True
    assert result["footprints_checked"] == 1
    assert result["no_courtyard"] == ["R2"]
    assert result["no_courtyard_count"] == 1


def test_out_of_outline_sees_circle_courtyard(tmp_path: Path) -> None:
    """A circle courtyard poking past the outline now counts (it was invisible
    to out_of_outline before RW-S3)."""
    board = (
        "(kicad_pcb\n"
        "\t(version 20241229)\n"
        '\t(generator "pcbnew")\n'
        '\t(net 0 "")\n'
        "\t(gr_rect (start 0 0) (end 60 60) "
        '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
        '\t(footprint "Test:U" (layer "F.Cu") (at 59 10)\n'
        '\t\t(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        "\t\t(fp_circle (center 0 0) (end 2 0) "
        '(stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd"))\n'
        '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        "\t)\n"
        ")\n"
    )
    p = tmp_path / "b.kicad_pcb"
    p.write_text(board, encoding="utf-8")
    metric = placement_metric(p)
    assert metric["out_of_outline_count"] == 1
    assert metric["no_courtyard"] == []
