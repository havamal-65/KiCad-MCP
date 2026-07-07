"""REQ-KTEST-101/102/103 — keep-out gate, geometry, parser, and writer (K1).

Covers AC1–AC7 (gate fixtures), the polygon-geometry edge cases, the keep-out
parser grammar variants, and the KWRITE file-backend zone transform (AC11).
Boards are tiny hand-authored ``.kicad_pcb`` strings in ``tmp_path`` — no KiCad
installation required.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_mcp.tools.drc import run_validate_placement_quality
from kicad_mcp.utils.gates import check_gate
from kicad_mcp.utils.keepout import (
    KeepoutArea,
    flatten_arc,
    parse_footprint_sides,
    parse_keepouts,
    point_in_polygon,
    rect_intersects_polygon,
    scan_board,
)

# ---------------------------------------------------------------------------
# Board builders (mirror tests/test_placement_quality_gate.py)
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(30 "B.CrtYd" user "B.Courtyard")
\t)
"""

_NETS = [(1, "SIG")]

#: All five keep-out object rules forbidden (the common rule-area shape).
_ALL_FORBIDDEN = {
    "tracks": "not_allowed", "vias": "not_allowed", "pads": "not_allowed",
    "copperpour": "not_allowed", "footprints": "not_allowed",
}


def _keepout_zone(
    polygon: list[tuple[float, float]],
    layers: str = '"F.Cu" "B.Cu"',
    rules: dict[str, str | None] | None = None,
    name: str | None = None,
    indent: str = "\t",
) -> str:
    """One ``(zone …)`` block. ``rules`` maps object → ``not_allowed`` /
    ``allowed`` / ``None`` (rule line absent). Default: all five forbidden."""
    resolved: dict[str, str | None] = dict(_ALL_FORBIDDEN)
    if rules is not None:
        resolved.update(rules)
    pts = " ".join(f"(xy {x} {y})" for x, y in polygon)
    name_clause = f'\n{indent}\t(name "{name}")' if name else ""
    rule_lines = "".join(
        f"{indent}\t\t({obj} {val})\n"
        for obj, val in resolved.items() if val is not None
    )
    return (
        f"{indent}(zone\n"
        f"{indent}\t(net 0)\n"
        f'{indent}\t(net_name "")\n'
        f"{indent}\t(layers {layers}){name_clause}\n"
        f"{indent}\t(hatch edge 0.5)\n"
        f"{indent}\t(keepout\n"
        f"{rule_lines}"
        f"{indent}\t)\n"
        f"{indent}\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        f"{indent}\t(polygon\n"
        f"{indent}\t\t(pts\n{indent}\t\t\t{pts}\n{indent}\t\t)\n"
        f"{indent}\t)\n"
        f"{indent})\n"
    )


def _footprint(
    ref: str,
    at: tuple[float, float, float],
    layer: str = "F.Cu",
    courtyard: tuple[float, float, float, float] | None = (-2.0, -2.0, 2.0, 2.0),
    embedded_zone: str | None = None,
) -> str:
    fx, fy, frot = at
    crtyd_layer = "B.CrtYd" if layer == "B.Cu" else "F.CrtYd"
    s = '\t(footprint "LibA:U"\n'
    s += f'\t\t(layer "{layer}")\n'
    s += f"\t\t(at {fx} {fy} {frot})\n"
    s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    s += f'\t\t(property "Value" "{ref}_val" (at 0 0 0) (layer "F.Fab"))\n'
    if courtyard is not None:
        cx0, cy0, cx1, cy1 = courtyard
        s += (
            f"\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) "
            f'(stroke (width 0.05) (type solid)) (fill no) (layer "{crtyd_layer}"))\n'
        )
    s += (
        '\t\t(pad "1" smd roundrect (at 0 0) (size 1 1) '
        f'(layers "{layer}" "F.Mask" "F.Paste") (net 1 "SIG"))\n'
    )
    if embedded_zone is not None:
        s += embedded_zone
    s += "\t)\n"
    return s


def _board(blocks: list[str]) -> str:
    s = _HEADER
    s += '\t(net 0 "")\n'
    for nid, nname in _NETS:
        s += f'\t(net {nid} "{nname}")\n'
    s += (
        "\t(gr_rect (start 0 0) (end 60 60) "
        '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
    )
    for b in blocks:
        s += b
    s += ")\n"
    return s


def _write(tmp_path: Path, content: str, name: str = "b.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _keepout_violations(result: dict) -> list[dict]:
    return [v for v in result["violations"] if v["type"] == "keepout_intrusion"]


#: An L-shaped (concave) outline: left arm x 10–20 spans y 10–40, bottom arm
#: y 10–20 spans x 10–40. The notch (x>20, y>20) is inside the bbox but
#: outside the polygon.
_L_SHAPE = [(10.0, 10.0), (40.0, 10.0), (40.0, 20.0), (20.0, 20.0),
            (20.0, 40.0), (10.0, 40.0)]


# ---------------------------------------------------------------------------
# Gate — AC1–AC7 (REQ-KTEST-101)
# ---------------------------------------------------------------------------

def test_ac1_board_level_intrusion_blocks(tmp_path: Path) -> None:
    zone = _keepout_zone([(15.0, 15.0), (25.0, 15.0), (25.0, 25.0), (15.0, 25.0)])
    board = _board([zone, _footprint("R1", (20.0, 20.0, 0.0))])
    p = _write(tmp_path, board)
    result = run_validate_placement_quality(p)
    assert result["passed"] is False
    kv = _keepout_violations(result)
    assert len(kv) == 1
    assert kv[0]["severity"] == "blocking"
    assert kv[0]["reference"] == "R1"
    assert kv[0]["keepout_origin"] == "board"
    assert any("R1" in a and "keep-out" in a for a in result["required_actions"])
    # First-class gate: this is exactly what autoroute's check_gate consumes.
    gap = check_gate(p, "validate_placement_quality")
    assert gap == {
        "ran": True, "passed": False, "violations": result["violations"],
    }


def test_ac2_embedded_keepout_flags_other_footprint(tmp_path: Path) -> None:
    # U1 at (20,20) carries a keep-out at board coords x 28–36, y 16–24
    # (embedded zones are stored board-absolute).
    ez = _keepout_zone(
        [(28.0, 16.0), (36.0, 16.0), (36.0, 24.0), (28.0, 24.0)],
        name="antenna", indent="\t\t",
    )
    board = _board([
        _footprint("U1", (20.0, 20.0, 0.0), embedded_zone=ez),
        _footprint("R1", (32.0, 20.0, 0.0)),
    ])
    result = run_validate_placement_quality(_write(tmp_path, board))
    assert result["passed"] is False
    kv = _keepout_violations(result)
    assert len(kv) == 1
    assert kv[0]["reference"] == "R1"
    assert kv[0]["keepout_origin"] == "embedded:U1"
    assert kv[0]["keepout_name"] == "antenna"


def test_ac3_own_embedded_keepout_is_exempt(tmp_path: Path) -> None:
    # U1's own courtyard (18–22 both axes) overlaps its own keep-out — exempt.
    ez = _keepout_zone(
        [(18.0, 18.0), (22.0, 18.0), (22.0, 22.0), (18.0, 22.0)],
        indent="\t\t",
    )
    board = _board([_footprint("U1", (20.0, 20.0, 0.0), embedded_zone=ez)])
    result = run_validate_placement_quality(_write(tmp_path, board))
    assert result["passed"] is True
    assert _keepout_violations(result) == []


def test_ac4_layer_mismatch_passes_wildcard_blocks(tmp_path: Path) -> None:
    poly = [(15.0, 15.0), (25.0, 15.0), (25.0, 25.0), (15.0, 25.0)]
    back_fp = _footprint("R1", (20.0, 20.0, 0.0), layer="B.Cu")
    # F.Cu-only keep-out vs a B.Cu footprint → no flag.
    board = _board([_keepout_zone(poly, layers='"F.Cu"'), back_fp])
    result = run_validate_placement_quality(_write(tmp_path, board, "a.kicad_pcb"))
    assert _keepout_violations(result) == []
    # *.Cu keep-out → flags the back-side footprint.
    board = _board([_keepout_zone(poly, layers='"*.Cu"'), back_fp])
    result = run_validate_placement_quality(_write(tmp_path, board, "b.kicad_pcb"))
    kv = _keepout_violations(result)
    assert len(kv) == 1 and kv[0]["reference"] == "R1"


def test_ac5_inside_bbox_outside_polygon_passes(tmp_path: Path) -> None:
    board = _board([
        _keepout_zone(_L_SHAPE),
        _footprint("R1", (32.0, 32.0, 0.0)),   # in the notch — outside the L
        _footprint("R2", (15.0, 30.0, 0.0)),   # in the left arm — inside the L
    ])
    result = run_validate_placement_quality(_write(tmp_path, board))
    kv = _keepout_violations(result)
    assert [v["reference"] for v in kv] == ["R2"]


def test_ac6_footprints_allowed_never_flags(tmp_path: Path) -> None:
    poly = [(15.0, 15.0), (25.0, 15.0), (25.0, 25.0), (15.0, 25.0)]
    fp = _footprint("R1", (20.0, 20.0, 0.0))
    for rules in (
        {"footprints": "allowed"},
        {"footprints": None},          # rule line absent entirely
    ):
        board = _board([_keepout_zone(poly, rules=rules), fp])
        result = run_validate_placement_quality(
            _write(tmp_path, board, f"allowed_{rules['footprints']}.kicad_pcb")
        )
        assert _keepout_violations(result) == []
        assert result["passed"] is True


def test_ac7_no_keepout_board_output_unchanged(tmp_path: Path) -> None:
    """A board with no keep-outs produces exactly today's gate output shape —
    metric bundle + empty violations/required_actions (REQ-KGATE-006)."""
    from kicad_mcp.utils.placement_metrics import placement_metric

    board = _board([_footprint("R1", (20.0, 20.0, 0.0))])
    p = _write(tmp_path, board)
    keepouts, _, warnings = scan_board(board)
    assert keepouts == [] and warnings == []
    result = run_validate_placement_quality(p)
    expected = {
        "passed": True,
        "placement_metric": placement_metric(p),
        "violations": [],
        "required_actions": [],
    }
    assert json.dumps(result, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_pour_zone_is_not_a_keepout(tmp_path: Path) -> None:
    """A filled copper zone (no keepout block) never participates."""
    pour = (
        "\t(zone\n"
        '\t\t(net 1)\n\t\t(net_name "SIG")\n\t\t(layers "F.Cu")\n'
        "\t\t(hatch edge 0.5)\n"
        "\t\t(polygon (pts (xy 0 0) (xy 60 0) (xy 60 60) (xy 0 60)))\n"
        "\t)\n"
    )
    board = _board([pour, _footprint("R1", (20.0, 20.0, 0.0))])
    result = run_validate_placement_quality(_write(tmp_path, board))
    assert result["passed"] is True
    assert _keepout_violations(result) == []


# ---------------------------------------------------------------------------
# Geometry (REQ-KTEST-102)
# ---------------------------------------------------------------------------

def test_point_in_polygon_concave() -> None:
    assert point_in_polygon((15.0, 30.0), tuple(_L_SHAPE)) is True     # left arm
    assert point_in_polygon((30.0, 15.0), tuple(_L_SHAPE)) is True     # bottom arm
    assert point_in_polygon((30.0, 30.0), tuple(_L_SHAPE)) is False    # notch
    assert point_in_polygon((50.0, 50.0), tuple(_L_SHAPE)) is False    # outside


def test_rect_fully_inside_polygon() -> None:
    poly = ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0))
    assert rect_intersects_polygon((40.0, 40.0, 60.0, 60.0), poly, 1e-6) is True


def test_polygon_fully_inside_rect() -> None:
    poly = ((40.0, 40.0), (60.0, 40.0), (60.0, 60.0), (40.0, 60.0))
    assert rect_intersects_polygon((0.0, 0.0, 100.0, 100.0), poly, 1e-6) is True


def test_touching_boundary_does_not_flag() -> None:
    poly = ((10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0))
    # Rect shares the x=10 edge exactly — positive-area semantics: no flag.
    assert rect_intersects_polygon((0.0, 0.0, 10.0, 10.0), poly, 1e-6) is False
    # A 2 µm push past the edge (beyond tolerance) flags.
    assert rect_intersects_polygon((0.0, 0.0, 10.0 + 2e-6, 10.0), poly, 1e-6) is True


def test_rect_inside_bbox_outside_concave_polygon() -> None:
    assert rect_intersects_polygon(
        (30.0, 30.0, 34.0, 34.0), tuple(_L_SHAPE), 1e-6,
    ) is False
    assert rect_intersects_polygon(
        (13.0, 28.0, 17.0, 32.0), tuple(_L_SHAPE), 1e-6,
    ) is True


def test_flatten_arc_deviation_bound() -> None:
    """Quarter circle r=10 about the origin: every emitted point sits on the
    circle, endpoints are exact, and each chord's sagitta ≤ the bound."""
    r = 10.0
    max_dev = 0.01
    s = (r, 0.0)
    m = (r * math.cos(math.pi / 4), r * math.sin(math.pi / 4))
    e = (0.0, r)
    pts = flatten_arc(s, m, e, max_dev)
    assert pts[0] == s and pts[-1] == e
    assert len(pts) > 3
    for x, y in pts:
        assert math.hypot(x, y) == pytest.approx(r, abs=1e-9)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        mid = (0.5 * (x1 + x2), 0.5 * (y1 + y2))
        sagitta = r - math.hypot(*mid)
        assert 0.0 <= sagitta <= max_dev + 1e-12


def test_flatten_arc_collinear_degenerates_to_segments() -> None:
    pts = flatten_arc((0.0, 0.0), (5.0, 0.0), (10.0, 0.0), 0.01)
    assert pts == ((0.0, 0.0), (5.0, 0.0), (10.0, 0.0))


def test_arc_outline_in_zone_is_flattened(tmp_path: Path) -> None:
    """A keep-out outline mixing xy and arc primitives parses to a polygon
    whose arc span is subdivided (not a 2-point chord)."""
    zone = (
        "\t(zone\n"
        '\t\t(net 0)\n\t\t(net_name "")\n\t\t(layers "F.Cu")\n'
        "\t\t(hatch edge 0.5)\n"
        "\t\t(keepout (footprints not_allowed))\n"
        "\t\t(polygon (pts (xy 10 10) "
        "(arc (start 30 10) (mid 40 20) (end 30 30)) (xy 10 30)))\n"
        "\t)\n"
    )
    keepouts, warnings = parse_keepouts(_board([zone]))
    assert warnings == []
    assert len(keepouts) == 1
    outline = keepouts[0].polygons[0]
    assert len(outline) > 5  # 3 fixed vertices + subdivided arc interior
    assert (10.0, 10.0) in outline and (10.0, 30.0) in outline


# ---------------------------------------------------------------------------
# Parser (REQ-KPARSE-*)
# ---------------------------------------------------------------------------

def test_layer_clause_variants() -> None:
    poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    cases = {
        '"F.Cu" "B.Cu"': {"F.Cu", "B.Cu"},
        '"F.Cu"': {"F.Cu"},
        '"*.Cu"': {"F.Cu", "B.Cu"},
        '"F&B.Cu"': {"F.Cu", "B.Cu"},
    }
    for clause, expected in cases.items():
        keepouts, _ = parse_keepouts(_board([_keepout_zone(poly, layers=clause)]))
        assert keepouts[0].layers == frozenset(expected), clause
    # Single-layer (layer "…") form (KiCad writes it for one-layer zones).
    zone = _keepout_zone(poly, layers='"F.Cu"').replace(
        '(layers "F.Cu")', '(layer "F.Cu")',
    )
    keepouts, _ = parse_keepouts(_board([zone]))
    assert keepouts[0].layers == frozenset({"F.Cu"})


def test_not_allowed_rules_parsed() -> None:
    poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    zone = _keepout_zone(poly, rules={
        "tracks": "allowed", "vias": None, "pads": "not_allowed",
        "copperpour": "allowed", "footprints": "not_allowed",
    })
    keepouts, _ = parse_keepouts(_board([zone]))
    assert keepouts[0].not_allowed == frozenset({"pads", "footprints"})
    assert keepouts[0].forbids_footprints is True


def test_embedded_zone_owner_and_sides() -> None:
    ez = _keepout_zone(
        [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)], indent="\t\t",
    )
    board = _board([
        _footprint("U1", (20.0, 20.0, 0.0), embedded_zone=ez),
        _footprint("R9", (40.0, 40.0, 0.0), layer="B.Cu"),
    ])
    keepouts, sides, warnings = scan_board(board)
    assert warnings == []
    assert [k.origin for k in keepouts] == ["embedded:U1"]
    assert sides == {"U1": "F.Cu", "R9": "B.Cu"}
    assert parse_footprint_sides(board) == sides


def test_malformed_zone_warns_not_crashes() -> None:
    zone = (
        "\t(zone\n"
        '\t\t(net 0)\n\t\t(layers "F.Cu")\n'
        "\t\t(keepout (footprints not_allowed))\n"
        "\t\t(polygon (pts (xy 1 1) (xy 2 2)))\n"   # <3 vertices
        "\t)\n"
    )
    keepouts, warnings = parse_keepouts(_board([zone]))
    assert keepouts == []
    assert len(warnings) == 2  # degenerate outline + zone skipped


def test_scan_is_deterministic() -> None:
    board = _board([
        _keepout_zone([(0.0, 0.0), (9.0, 0.0), (9.0, 9.0), (0.0, 9.0)]),
        _footprint("R1", (20.0, 20.0, 0.0)),
    ])
    assert scan_board(board) == scan_board(board)


# ---------------------------------------------------------------------------
# KWRITE — file-backend zone transform (REQ-KTEST-103, AC11)
# ---------------------------------------------------------------------------

_FIXTURE_MOD = (
    Path(__file__).parent / "fixtures" / "footprints" / "keepout_module.kicad_mod"
)

#: The fixture's zone corners in footprint-local coordinates.
_LOCAL_ZONE = [(-3.0, 4.0), (3.0, 4.0), (3.0, 8.0), (-3.0, 8.0)]


def _rot(p: tuple[float, float], deg: float) -> tuple[float, float]:
    # KiCad convention: positive rotation = CCW on screen = CW in y-down file
    # coords, (x,y) -> (x*c + y*s, -x*s + y*c). Matches pcbnew-written
    # geometry (tests/test_rotation_convention.py).
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def _expected_zone(x: float, y: float, deg: float) -> set[tuple[float, float]]:
    out = set()
    for p in _LOCAL_ZONE:
        rx, ry = _rot(p, deg)
        out.add((round(rx + x, 6), round(ry + y, 6)))
    return out


def _place_fixture(
    tmp_path: Path, monkeypatch, ref: str, x: float, y: float, rotation: float,
    name: str = "w.kicad_pcb",
) -> Path:
    from kicad_mcp.backends import file_backend

    mod_text = _FIXTURE_MOD.read_text(encoding="utf-8")
    monkeypatch.setattr(file_backend, "_load_kicad_mod", lambda *a, **k: mod_text)
    p = _write(tmp_path, _board([]), name)
    file_backend.FileBoardOps().place_component(
        p, ref, "Test:KeepoutModule", x, y, rotation=rotation,
    )
    return p


def _placed_zone_points(p: Path) -> set[tuple[float, float]]:
    keepouts, _ = parse_keepouts(p.read_text(encoding="utf-8"))
    assert len(keepouts) == 1
    (outline,) = keepouts[0].polygons
    return {(round(x, 6), round(y, 6)) for x, y in outline}


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
def test_place_transforms_embedded_zone(tmp_path, monkeypatch, rotation) -> None:
    p = _place_fixture(tmp_path, monkeypatch, "U1", 50.0, 40.0, rotation)
    assert _placed_zone_points(p) == _expected_zone(50.0, 40.0, rotation)
    keepouts, _ = parse_keepouts(p.read_text(encoding="utf-8"))
    assert keepouts[0].origin == "embedded:U1"
    assert keepouts[0].forbids_footprints is True


def test_move_retransforms_embedded_zone(tmp_path, monkeypatch) -> None:
    """Moving (and rotating) a placed footprint carries its zone along —
    equivalent to having placed it at the destination directly."""
    from kicad_mcp.backends import file_backend

    p = _place_fixture(tmp_path, monkeypatch, "U1", 50.0, 40.0, 0.0)
    file_backend.FileBoardOps().move_component(p, "U1", 30.0, 20.0, rotation=90.0)
    assert _placed_zone_points(p) == _expected_zone(30.0, 20.0, 90.0)
    # And back through a second move (regression against cumulative drift).
    file_backend.FileBoardOps().move_component(p, "U1", 50.0, 40.0, rotation=0.0)
    assert _placed_zone_points(p) == _expected_zone(50.0, 40.0, 0.0)


def test_zone_free_footprint_move_touches_only_at_clause(tmp_path) -> None:
    from kicad_mcp.backends import file_backend

    board = _board([_footprint("R1", (20.0, 20.0, 0.0))])
    p = _write(tmp_path, board)
    before = p.read_text(encoding="utf-8")
    file_backend.FileBoardOps().move_component(p, "R1", 25.0, 25.0)
    after = p.read_text(encoding="utf-8")
    diff = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b
    ]
    assert diff == [("\t\t(at 20.0 20.0 0.0)", "\t\t(at 25.0 25.0)")]


def test_file_placed_module_end_to_end_gate(tmp_path, monkeypatch) -> None:
    """AC2 + AC11 end to end: a file-backend-placed module's keep-out sits at
    board coordinates, and the gate flags an intruder while exempting the
    owner."""
    p = _place_fixture(tmp_path, monkeypatch, "U1", 20.0, 20.0, 0.0)
    # Zone now spans x 17–23, y 24–28 (board frame). Owner alone passes.
    assert run_validate_placement_quality(p)["passed"] is True
    # An intruder at (20, 26) with a ±2 courtyard lands inside it.
    content = p.read_text(encoding="utf-8")
    content = content[:content.rfind(")")] + _footprint(
        "R1", (20.0, 26.0, 0.0),
    ) + content[content.rfind(")"):]
    p.write_text(content, encoding="utf-8")
    result = run_validate_placement_quality(p)
    assert result["passed"] is False
    kv = _keepout_violations(result)
    assert [v["reference"] for v in kv] == ["R1"]
    assert kv[0]["keepout_origin"] == "embedded:U1"


def test_keepout_area_dataclass_surface() -> None:
    area = KeepoutArea(
        origin="board", name=None, layers=frozenset({"F.Cu"}),
        not_allowed=frozenset({"tracks"}), polygons=(),
    )
    assert area.forbids_footprints is False
