"""REQ-KTEST-201 — placement-engine keep-out avoidance (K2, AC9/AC10).

The engine must never *choose* a position violating a footprint-forbidding
keep-out (AC9), and embedded keep-outs must travel with their owner during
planning — checked at the owner's candidate position, never its stale on-file
position (AC10). Boards are tiny hand-authored ``.kicad_pcb`` strings; direct
tests build engine records inline. No KiCad installation required.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_mcp.utils.keepout import (
    KeepoutArea,
    find_keepout_intrusions,
    rect_intersects_polygon,
    scan_board,
    transform_polygon,
    untransform_polygon,
)
from kicad_mcp.utils.placement_engine import (
    ROLE_PASSIVE,
    PadLocal,
    PartRecord,
    _board_box,
    _improve,
    _KeepoutFilter,
    compute_net_aware_plan,
    legalize,
    normalize_orientations,
    read_board_keepouts,
    read_part_records,
)

# ---------------------------------------------------------------------------
# Board builders (engine flavour: pads carry nets so attractors pull)
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


def _keepout_zone(
    polygon: list[tuple[float, float]],
    layers: str = '"F.Cu" "B.Cu"',
    indent: str = "\t",
) -> str:
    pts = " ".join(f"(xy {x} {y})" for x, y in polygon)
    return (
        f"{indent}(zone\n"
        f"{indent}\t(net 0)\n"
        f'{indent}\t(net_name "")\n'
        f"{indent}\t(layers {layers})\n"
        f"{indent}\t(hatch edge 0.5)\n"
        f"{indent}\t(keepout (footprints not_allowed))\n"
        f"{indent}\t(polygon (pts {pts}))\n"
        f"{indent})\n"
    )


def _fp(
    ref: str,
    at: tuple[float, float, float],
    pads: list[tuple[str, float, float, int, str]],
    courtyard: tuple[float, float, float, float] = (-2.0, -2.0, 2.0, 2.0),
    layer: str = "F.Cu",
    embedded_zone: str | None = None,
) -> str:
    fx, fy, frot = at
    crtyd = "B.CrtYd" if layer == "B.Cu" else "F.CrtYd"
    s = '\t(footprint "LibA:U"\n'
    s += f'\t\t(layer "{layer}")\n'
    s += f"\t\t(at {fx} {fy} {frot})\n"
    s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    cx0, cy0, cx1, cy1 = courtyard
    s += (
        f"\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) "
        f'(stroke (width 0.05) (type solid)) (fill no) (layer "{crtyd}"))\n'
    )
    for name, dx, dy, net_id, net_name in pads:
        net = f' (net {net_id} "{net_name}")' if net_id else ""
        s += (
            f'\t\t(pad "{name}" smd roundrect (at {dx} {dy}) (size 1 1) '
            f'(layers "{layer}"){net}\n\t\t)\n'
        )
    if embedded_zone is not None:
        s += embedded_zone
    s += "\t)\n"
    return s


def _board(blocks: list[str], nets: list[tuple[int, str]]) -> str:
    s = _HEADER
    s += '\t(net 0 "")\n'
    for nid, nname in nets:
        s += f'\t(net {nid} "{nname}")\n'
    s += (
        "\t(gr_rect (start 0 0) (end 60 60) "
        '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
    )
    for b in blocks:
        s += b
    s += ")\n"
    return s


def _plan(
    content: str,
    anchors: list[str] | None = None,
    use_keepouts: bool = True,
    clearance: float = 0.5,
):
    """compute_net_aware_plan over board text → (positions, warnings, parts,
    sides, forbidding keep-outs). ``positions`` includes anchors."""
    parts = read_part_records(content)
    keepouts, sides, _ = scan_board(content)
    forbidding = tuple(k for k in keepouts if k.forbids_footprints)
    items, warnings, _area = compute_net_aware_plan(
        parts, 0.0, 0.0, 60.0, 60.0, clearance, anchors or [],
        keepouts=forbidding if use_keepouts else (),
        part_sides=sides,
    )
    by_ref = {p["ref"]: p for p in parts}
    positions: dict[str, tuple[float, float, float]] = {
        r: by_ref[r]["pos"] for r in (anchors or []) if r in by_ref
    }
    for ref, x, y, rot in items:
        positions[ref] = (x, y, rot)
    return positions, warnings, parts, sides, forbidding


def _final_intrusions(
    parts: list[PartRecord],
    positions: dict[str, tuple[float, float, float]],
    anchors: list[str],
    forbidding: tuple[KeepoutArea, ...],
    sides: dict[str, str],
) -> list[dict]:
    """The gate's checker on the plan's final absolute geometry (movable-owner
    zones transformed to their final positions)."""
    by_ref = {p["ref"]: p for p in parts}
    kf = _KeepoutFilter(parts, frozenset(anchors), forbidding, sides)
    courtyards = {}
    for ref, pos in positions.items():
        box = _board_box(by_ref[ref], (pos[0], pos[1]), pos[2])
        courtyards[ref] = {
            "xmin": box[0], "ymin": box[1], "xmax": box[2], "ymax": box[3],
        }
    return find_keepout_intrusions(courtyards, sides, kf.materialize(positions))


def _unresolved(warnings: list[dict]) -> list[dict]:
    return [w for w in warnings if w.get("type") == "keepout_unresolved"]


def _box_at(
    parts: list[PartRecord], ref: str, pos: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    by_ref = {p["ref"]: p for p in parts}
    return _board_box(by_ref[ref], (pos[0], pos[1]), pos[2])


def _rec(
    ref: str,
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
    courtyard: tuple[float, float, float, float] = (-2.0, -2.0, 2.0, 2.0),
    pads: list[tuple[str, int, str, float, float]] | None = None,
    lib_id: str = "LibA:U",
) -> PartRecord:
    plist = [
        PadLocal(pad=n, net_id=i, net_name=nm, dx=dx, dy=dy)
        for n, i, nm, dx, dy in (pads or [])
    ]
    return PartRecord(
        ref=ref, lib_id=lib_id, cluster_key="", pad_count=len(plist),
        courtyard=courtyard, pads=plist, pos=pos,
    )


def _area(
    poly: list[tuple[float, float]],
    layers: set[str] | None = None,
    origin: str = "board",
) -> KeepoutArea:
    return KeepoutArea(
        origin=origin, name=None,
        layers=frozenset(layers or {"F.Cu", "B.Cu"}),
        not_allowed=frozenset({"footprints"}),
        polygons=(tuple(poly),),
    )


_SQUARE = [(4.0, 22.0), (20.0, 22.0), (20.0, 38.0), (4.0, 38.0)]


# ---------------------------------------------------------------------------
# Transform round-trip (spec test 12)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rot", [0.0, 90.0, 180.0, 270.0, 37.5])
def test_transform_untransform_roundtrip(rot: float) -> None:
    poly = ((-3.0, 4.0), (3.0, 4.0), (3.0, 8.0), (-3.0, 8.0), (0.0, 6.5))
    fwd = transform_polygon(poly, 31.7, -12.25, rot)
    back = untransform_polygon(fwd, 31.7, -12.25, rot)
    for (ax, ay), (bx, by) in zip(poly, back):
        assert math.isclose(ax, bx, abs_tol=1e-9)
        assert math.isclose(ay, by, abs_tol=1e-9)


def test_transform_matches_kwrite_convention() -> None:
    # (3, 0) rotated 90° must land at (0, 3) relative to the origin — the
    # _parse_placed_courtyards / KWRITE convention (live-verified in P4/K1).
    (px, py), = transform_polygon(((3.0, 0.0),), 10.0, 20.0, 90.0)
    assert math.isclose(px, 10.0, abs_tol=1e-9)
    assert math.isclose(py, 23.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# _KeepoutFilter predicate (direct)
# ---------------------------------------------------------------------------

def test_predicate_static_layer_match_and_self_exemption() -> None:
    parts = [_rec("U1", pos=(30.0, 30.0, 0.0)), _rec("R9")]
    zone = _area([(28.0, 28.0), (36.0, 28.0), (36.0, 36.0), (28.0, 36.0)],
                 layers={"F.Cu"}, origin="embedded:U1")
    kf = _KeepoutFilter(parts, frozenset({"U1"}), (zone,), {"U1": "F.Cu", "R9": "F.Cu"})
    inside = (30.0, 30.0, 0.0)
    # R9's box inside the zone conflicts; U1 is exempt from its own zone.
    assert kf.conflicts({"R9": inside}, {"U1": (30.0, 30.0, 0.0), "R9": inside})
    assert not kf.conflicts({"U1": inside}, {"U1": inside})
    # A B.Cu part never matches an F.Cu-only zone.
    kf_b = _KeepoutFilter(parts, frozenset({"U1"}), (zone,), {"U1": "F.Cu", "R9": "B.Cu"})
    assert not kf_b.conflicts({"R9": inside}, {"U1": (30.0, 30.0, 0.0), "R9": inside})


def test_predicate_movable_owner_zone_tracks_candidate() -> None:
    # U1's zone sits at board x 44–56, y 44–56 on file (local ±6). Checked at a
    # candidate position, the zone is where the owner is going, not where it was.
    parts = [_rec("U1", pos=(50.0, 50.0, 0.0)), _rec("R1")]
    zone = _area([(44.0, 44.0), (56.0, 44.0), (56.0, 56.0), (44.0, 56.0)],
                 origin="embedded:U1")
    kf = _KeepoutFilter(parts, frozenset(), (zone,), None)
    # R1 at the STALE zone location, with U1 moved away → no conflict.
    assert not kf.conflicts(
        {"R1": (50.0, 50.0, 0.0)},
        {"U1": (10.0, 10.0, 0.0), "R1": (50.0, 50.0, 0.0)},
    )
    # R1 at the owner's NEW zone location → conflict.
    assert kf.conflicts(
        {"R1": (10.0, 10.0, 0.0)},
        {"U1": (10.0, 10.0, 0.0), "R1": (10.0, 10.0, 0.0)},
    )
    # Direction (b): the OWNER's candidate is rejected when its zone would
    # land on an already-placed part.
    assert kf.conflicts(
        {"U1": (10.0, 10.0, 0.0)},
        {"U1": (10.0, 10.0, 0.0), "R1": (12.0, 12.0, 0.0)},
    )
    # Owner absent from pos_all → its zone constrains nothing yet.
    assert not kf.conflicts({"R1": (50.0, 50.0, 0.0)}, {"R1": (50.0, 50.0, 0.0)})


def test_predicate_empty_is_noop() -> None:
    kf = _KeepoutFilter([_rec("R1")], frozenset(), (), None)
    assert kf.empty
    assert not kf.conflicts({"R1": (0.0, 0.0, 0.0)}, {"R1": (0.0, 0.0, 0.0)})


# ---------------------------------------------------------------------------
# AC9 — the engine never places into a keep-out (spec tests 1–3)
# ---------------------------------------------------------------------------

def _ac9_board(zone_layers: str = '"F.Cu" "B.Cu"', part_layer: str = "F.Cu") -> str:
    return _board([
        _keepout_zone(_SQUARE, layers=zone_layers),
        _fp("J1", (10.0, 30.0, 0.0), [("1", 0.0, 0.0, 1, "S")], layer=part_layer),
        _fp("R1", (55.0, 5.0, 0.0), [("1", 0.0, 0.0, 1, "S")], layer=part_layer),
    ], nets=[(1, "S")])


def test_ac9_board_level_keepout_avoided() -> None:
    content = _ac9_board()
    # Without the filter the attractor drags R1 straight into the zone —
    # proving the fixture bites.
    pos0, _, parts, sides, forbidding = _plan(content, ["J1"], use_keepouts=False)
    assert [v["reference"] for v in _final_intrusions(
        parts, pos0, ["J1"], forbidding, sides,
    ) if v["reference"] != "J1"] == ["R1"]
    # With the filter: R1 lands outside; audit clean; no unresolved warning.
    pos, warnings, parts, sides, forbidding = _plan(content, ["J1"])
    assert _unresolved(warnings) == []
    assert [v["reference"] for v in _final_intrusions(
        parts, pos, ["J1"], forbidding, sides,
    ) if v["reference"] != "J1"] == []
    assert not rect_intersects_polygon(
        _box_at(parts, "R1", pos["R1"]), tuple(_SQUARE), 1e-6,
    )


def test_ac9_layer_mismatch_not_constrained() -> None:
    # F.Cu-only zone, B.Cu parts: the filter must not constrain them at all —
    # identical plan with and without keep-outs, landing inside the zone.
    content = _ac9_board(zone_layers='"F.Cu"', part_layer="B.Cu")
    pos_with, warnings, parts, _, _ = _plan(content, ["J1"])
    pos_without, _, _, _, _ = _plan(content, ["J1"], use_keepouts=False)
    assert pos_with == pos_without
    assert _unresolved(warnings) == []
    assert rect_intersects_polygon(
        _box_at(parts, "R1", pos_with["R1"]), tuple(_SQUARE), 1e-6,
    )


def test_ac9_anchored_owner_embedded_zone_avoided() -> None:
    # Anchored U1 at (30, 30) carries a keep-out at board y 34–40 below it
    # (the ESP32-antenna shape). R1 pulls toward U1 but never enters the zone.
    ez = _keepout_zone(
        [(24.0, 34.0), (36.0, 34.0), (36.0, 40.0), (24.0, 40.0)], indent="\t\t",
    )
    zone_poly = ((24.0, 34.0), (36.0, 34.0), (36.0, 40.0), (24.0, 40.0))
    content = _board([
        _fp("U1", (30.0, 30.0, 0.0),
            [("1", 0.0, 3.5, 1, "S")], embedded_zone=ez),
        _fp("R1", (5.0, 55.0, 0.0), [("1", 0.0, 0.0, 1, "S")]),
    ], nets=[(1, "S")])
    # The attractor (U1's pad at y 33.5) pulls R1 into the zone without the
    # filter.
    pos0, _, parts, sides, forbidding = _plan(content, ["U1"], use_keepouts=False)
    assert rect_intersects_polygon(_box_at(parts, "R1", pos0["R1"]), zone_poly, 1e-6)
    pos, warnings, parts, sides, forbidding = _plan(content, ["U1"])
    assert _unresolved(warnings) == []
    assert not rect_intersects_polygon(
        _box_at(parts, "R1", pos["R1"]), zone_poly, 1e-6,
    )
    assert _final_intrusions(parts, pos, ["U1"], forbidding, sides) == []


# ---------------------------------------------------------------------------
# AC10 — embedded zones travel with their owner (spec tests 4–5)
# ---------------------------------------------------------------------------

def _ac10_board() -> str:
    # U1 on file at (15, 10) with a huge embedded zone (local ±10 → stale board
    # x 5–25, y 0–20, covering anchor J1's neighbourhood). U1 is pulled to J2
    # at (45, 45); R1 is pulled to J1 at (15, 15).
    ez = _keepout_zone(
        [(5.0, 0.0), (25.0, 0.0), (25.0, 20.0), (5.0, 20.0)], indent="\t\t",
    )
    return _board([
        _fp("J1", (15.0, 15.0, 0.0), [("1", 0.0, 0.0, 1, "A")]),
        _fp("J2", (45.0, 45.0, 0.0), [("1", 0.0, 0.0, 2, "B")]),
        _fp("U1", (15.0, 10.0, 0.0), [("1", 0.0, 0.0, 2, "B")], embedded_zone=ez),
        _fp("R1", (55.0, 55.0, 0.0), [("1", 0.0, 0.0, 1, "A")]),
    ], nets=[(1, "A"), (2, "B")])


def test_ac10_zone_travels_with_relocated_owner() -> None:
    stale_zone = ((5.0, 0.0), (25.0, 0.0), (25.0, 20.0), (5.0, 20.0))
    pos, warnings, parts, sides, forbidding = _plan(_ac10_board(), ["J1", "J2"])
    assert _unresolved(warnings) == []
    # The owner moved (toward J2) — the stale on-file spot is history.
    ux, uy, urot = pos["U1"]
    assert (ux, uy) != (15.0, 10.0)
    # R1 settled next to J1, INSIDE the stale zone footprint: candidate-frame
    # checking — the zone was never enforced at its stale location.
    assert rect_intersects_polygon(_box_at(parts, "R1", pos["R1"]), stale_zone, 1e-6)
    # At the owner's final position the zone covers nobody (gate-clean).
    local = untransform_polygon(stale_zone, 15.0, 10.0, 0.0)
    final_zone = transform_polygon(local, ux, uy, urot)
    for other in ("R1", "J1", "J2"):
        assert not rect_intersects_polygon(
            _box_at(parts, other, pos[other]), final_zone, 1e-6,
        ), other
    assert _final_intrusions(parts, pos, ["J1", "J2"], forbidding, sides) == []


def test_ac10_late_owner_does_not_drop_zone_on_placed_parts() -> None:
    # R1 (higher in-cluster degree) places before owner U1; U1's ±6 zone must
    # not land on R1 or the anchor J1 (predicate direction (b)).
    ez = _keepout_zone(
        [(44.0, 44.0), (56.0, 44.0), (56.0, 56.0), (44.0, 56.0)], indent="\t\t",
    )
    content = _board([
        _fp("J1", (20.0, 20.0, 0.0),
            [("1", -1.0, 0.0, 1, "S1"), ("2", 1.0, 0.0, 2, "S2")]),
        _fp("R1", (55.0, 5.0, 0.0),
            [("1", -0.5, 0.0, 1, "S1"), ("2", 0.5, 0.0, 2, "S2")]),
        _fp("U1", (50.0, 50.0, 0.0), [("1", 0.0, 0.0, 1, "S1")], embedded_zone=ez),
    ], nets=[(1, "S1"), (2, "S2")])
    pos, warnings, parts, sides, forbidding = _plan(content, ["J1"])
    assert _unresolved(warnings) == []
    local = untransform_polygon(
        ((44.0, 44.0), (56.0, 44.0), (56.0, 56.0), (44.0, 56.0)), 50.0, 50.0, 0.0,
    )
    ux, uy, urot = pos["U1"]
    final_zone = transform_polygon(local, ux, uy, urot)
    for other in ("R1", "J1"):
        assert not rect_intersects_polygon(
            _box_at(parts, other, pos[other]), final_zone, 1e-6,
        ), other
    assert _final_intrusions(parts, pos, ["J1"], forbidding, sides) == []


# ---------------------------------------------------------------------------
# Never-worse + determinism (spec tests 7–8)
# ---------------------------------------------------------------------------

def test_no_legal_position_places_anyway_and_warns() -> None:
    # A keep-out covering the whole board: R1 still gets a position (never
    # dropped), planning terminates, and the audit names it.
    content = _board([
        _keepout_zone([(0.0, 0.0), (60.0, 0.0), (60.0, 60.0), (0.0, 60.0)]),
        _fp("R1", (30.0, 30.0, 0.0), [("1", 0.0, 0.0, 0, "")]),
    ], nets=[])
    pos, warnings, _, _, _ = _plan(content)
    assert "R1" in pos
    unresolved = _unresolved(warnings)
    assert len(unresolved) == 1
    assert unresolved[0]["ref"] == "R1"
    assert unresolved[0]["keepout_origin"] == "board"


def test_determinism_same_input_same_plan() -> None:
    content = _ac10_board()
    first = _plan(content, ["J1", "J2"])
    second = _plan(content, ["J1", "J2"])
    assert first[0] == second[0]
    assert first[1] == second[1]


def test_far_keepout_does_not_perturb_plan() -> None:
    # A keep-out far from all activity: filtered plan == unfiltered plan
    # (no gratuitous perturbation; backward-compat beyond the empty default).
    content = _board([
        _keepout_zone([(45.0, 45.0), (58.0, 45.0), (58.0, 58.0), (45.0, 58.0)]),
        _fp("J1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "S")]),
        _fp("R1", (30.0, 5.0, 0.0), [("1", 0.0, 0.0, 1, "S")]),
    ], nets=[(1, "S")])
    pos_with, warnings, _, _, _ = _plan(content, ["J1"])
    pos_without, _, _, _, _ = _plan(content, ["J1"], use_keepouts=False)
    assert pos_with == pos_without
    assert _unresolved(warnings) == []


# ---------------------------------------------------------------------------
# Downstream passes preserve avoidance (spec test 9)
# ---------------------------------------------------------------------------

def test_legalize_reseats_outside_keepout() -> None:
    # R1/R2 stacked at (10, 10); zone covers x 0–9. Without the filter the
    # nearest free slot is (6, 6) — inside the zone. With it: (14, 6).
    parts = [_rec("R1"), _rec("R2")]
    plan = {"R1": (10.0, 10.0, 0.0), "R2": (10.0, 10.0, 0.0)}
    zone = _area([(0.0, 0.0), (9.0, 0.0), (9.0, 20.0), (0.0, 20.0)])
    kf = _KeepoutFilter(parts, frozenset(), (zone,), None)
    bare, bare_warn = legalize(parts, dict(plan), {}, (0.0, 0.0, 20.0, 20.0), 0.0)
    assert bare["R1"] == (6.0, 6.0, 0.0)
    filtered, warn = legalize(
        parts, dict(plan), {}, (0.0, 0.0, 20.0, 20.0), 0.0, keepout_filter=kf,
    )
    assert warn == [] and bare_warn == []
    assert filtered["R1"] == (14.0, 6.0, 0.0)
    assert not rect_intersects_polygon(
        _box_at(parts, "R1", filtered["R1"]), zone.polygons[0], 1e-6,
    )


def test_improve_never_steps_into_keepout() -> None:
    # Moving R1 toward the anchor strictly reduces HPWL but crosses into the
    # zone (x ≤ 7.9): with the filter the improvement is rejected.
    parts = [
        _rec("J1", pos=(2.0, 10.0, 0.0), pads=[("1", 1, "S", 0.0, 0.0)]),
        _rec("R1", pads=[("1", 1, "S", 0.0, 0.0)]),
    ]
    anchors = {"J1": (2.0, 10.0, 0.0)}
    plan = {"R1": (10.0, 10.0, 0.0)}
    zone = _area([(0.0, 8.0), (7.9, 8.0), (7.9, 12.0), (0.0, 12.0)])
    kf = _KeepoutFilter(parts, frozenset(anchors), (zone,), None)
    bare = _improve(parts, dict(plan), anchors, (0.0, 0.0, 30.0, 30.0))
    assert bare["R1"][0] < 10.0  # the unfiltered pass does walk left
    held = _improve(
        parts, dict(plan), anchors, (0.0, 0.0, 30.0, 30.0),
        keepout_filter=kf,
    )
    assert held["R1"] == (10.0, 10.0, 0.0)


def test_orientation_flip_never_swings_into_keepout() -> None:
    # Rotating R1 to 270° strictly reduces HPWL, but the rotated courtyard
    # (tall) would clip the zone at y 5–8: the flip is rejected with the
    # filter, taken without it.
    parts = [
        _rec("J1", pos=(10.0, 2.0, 0.0), pads=[("1", 1, "S", 0.0, 0.0)]),
        _rec("R1", courtyard=(-4.0, -1.0, 4.0, 1.0),
             pads=[("1", 1, "S", 3.0, 0.0), ("2", 2, "T", -3.0, 0.0)]),
    ]
    roles = {"R1": ROLE_PASSIVE}
    anchors = {"J1": (10.0, 2.0, 0.0)}
    plan = {"R1": (10.0, 10.0, 0.0)}
    zone = _area([(8.0, 5.0), (12.0, 5.0), (12.0, 8.0), (8.0, 8.0)])
    kf = _KeepoutFilter(parts, frozenset(anchors), (zone,), None)
    bare, _ = normalize_orientations(
        parts, dict(plan), roles, anchors, (0.0, 0.0, 30.0, 30.0),
    )
    assert bare["R1"][2] == 270.0
    held, _ = normalize_orientations(
        parts, dict(plan), roles, anchors, (0.0, 0.0, 30.0, 30.0),
        keepout_filter=kf,
    )
    assert held["R1"] == (10.0, 10.0, 0.0)


# ---------------------------------------------------------------------------
# End-to-end: file-backend auto_place → gate-clean (spec test 11)
# ---------------------------------------------------------------------------

_FIXTURE_MOD = (
    Path(__file__).parent / "fixtures" / "footprints" / "keepout_module.kicad_mod"
)


def test_auto_place_end_to_end_gate_clean(tmp_path: Path, monkeypatch) -> None:
    """engine-legal ⇒ gate-clean, closed loop: the file backend places the
    keep-out module (KWRITE transforms its zone), auto_place lays out the
    passives around it, and the K1 gate reports zero keepout_intrusion."""
    from kicad_mcp.backends import file_backend
    from kicad_mcp.tools.drc import run_validate_placement_quality

    mod_text = _FIXTURE_MOD.read_text(encoding="utf-8")
    monkeypatch.setattr(file_backend, "_load_kicad_mod", lambda *a, **k: mod_text)
    p = tmp_path / "k2.kicad_pcb"
    p.write_text(_board([
        _fp("R1", (5.0, 55.0, 0.0), [("1", 0.0, 0.0, 1, "S")]),
        _fp("R2", (55.0, 55.0, 0.0), [("1", 0.0, 0.0, 1, "S")]),
    ], nets=[(1, "S")]), encoding="utf-8")
    ops = file_backend.FileBoardOps()
    # Anchored owner: its zone (board y 34–38 under the module) is static.
    ops.place_component(p, "U1", "Test:KeepoutModule", 30.0, 30.0)

    keepouts, sides = read_board_keepouts(p)
    assert [k.origin for k in keepouts] == ["embedded:U1"]
    assert sides["U1"] == "F.Cu"

    result = ops.auto_place(
        p, board_x=0.0, board_y=0.0, board_width=60.0, board_height=60.0,
        clearance_mm=0.5, anchors=["U1"], strategy="net_aware",
    )
    assert result["components_placed"] == 2
    assert not _unresolved(list(result["warnings"]))

    gate = run_validate_placement_quality(p)
    assert [
        v for v in gate["violations"] if v["type"] == "keepout_intrusion"
    ] == []
    assert gate["passed"] is True
