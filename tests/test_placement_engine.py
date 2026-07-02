"""Unit tests for the net-aware placement engine (Sprint P2).

Records in → plan out, no KiCad install (spec-p2 §2.1). Covers:
  * classification (REQ-CLASS-001..006) incl. connector-never-IC and the
    decoupling-cap two-pass that needs ICs resolved first;
  * decoupling-cap pairing — single IC, load-balanced multi-IC, bulk-cap-no-IC
    stays passive (REQ-DECAP-001/002/004);
  * clustering — explicit override wins, graph components, singletons, caps
    travel with their IC (REQ-CLUSTER-001..003);
  * constructive placement — legal (no overlap, in outline), anchors preserved,
    decaps within DECAP_MAX_MM, deterministic (REQ-PROX-*, REQ-DET-002, AC7);
  * legalizer — separates overlaps and terminates with a structured warning on
    an impossible fixture rather than looping (REQ-LEGAL-001..002);
  * the board-text parser ``read_part_records``.
"""

from __future__ import annotations

from kicad_mcp.utils import placement_engine as e
from kicad_mcp.utils.placement_engine import (
    ROLE_CONNECTOR,
    ROLE_CRYSTAL,
    ROLE_DECOUPLING_CAP,
    ROLE_IC,
    ROLE_OTHER,
    ROLE_PASSIVE,
    PadLocal,
    PartRecord,
)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _pad(name: str, nid: int, net: str, dx: float, dy: float) -> PadLocal:
    return PadLocal(pad=name, net_id=nid, net_name=net, dx=dx, dy=dy)


def _part(
    ref: str,
    lib: str,
    pads: list[PadLocal],
    courtyard: tuple[float, float, float, float] = (-1.0, -1.0, 1.0, 1.0),
    cluster_key: str = "",
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
    pad_count: int | None = None,
) -> PartRecord:
    return PartRecord(
        ref=ref,
        lib_id=lib,
        cluster_key=cluster_key,
        pad_count=pad_count if pad_count is not None else len(pads),
        courtyard=courtyard,
        pads=pads,
        pos=pos,
    )


def _ic(ref: str = "U1") -> PartRecord:
    return _part(
        ref, "Package_SO:SOIC-8",
        [
            _pad("1", 1, "IN0", -1, 0), _pad("2", 2, "IN1", -0.5, 0),
            _pad("4", 6, "GND", 0, 0), _pad("5", 3, "OUT0", 0.5, 0),
            _pad("6", 4, "OUT1", 1, 0), _pad("8", 5, "VCC", -1, 1),
        ],
        courtyard=(-2, -2, 2, 2),
    )


def _decap(ref: str, rail: str = "VCC") -> PartRecord:
    return _part(
        ref, "Capacitor_SMD:C_0402",
        [_pad("1", 5, rail, -0.5, 0), _pad("2", 6, "GND", 0.5, 0)],
        courtyard=(-0.5, -0.5, 0.5, 0.5),
    )


def _boxes_for(parts: list[PartRecord], plan: dict[str, tuple[float, float, float]]):
    by_ref = {p["ref"]: p for p in parts}
    return {
        r: e._board_box(by_ref[r], (pos[0], pos[1]), pos[2])
        for r, pos in plan.items()
    }


def _count_overlaps(parts: list[PartRecord], plan) -> int:
    boxes = _boxes_for(parts, plan)
    refs = sorted(boxes)
    n = 0
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            if e._boxes_overlap(boxes[refs[i]], boxes[refs[j]], gap=0.0):
                n += 1
    return n


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_classification_roles() -> None:
    parts = [
        _ic("U1"),
        _part("J1", "Connector:Conn_01x02",
              [_pad("1", 1, "IN0", 0, 0), _pad("2", 2, "IN1", 1, 0)]),
        _decap("C1"),
        _part("Y1", "Crystal:Crystal_SMD",
              [_pad("1", 7, "XIN", 0, 0), _pad("2", 8, "XOUT", 1, 0)]),
        _part("R1", "Resistor_SMD:R_0402",
              [_pad("1", 1, "IN0", 0, 0), _pad("2", 9, "NET9", 1, 0)]),
        _part("TP1", "TestPoint:TP",
              [_pad("1", 9, "NET9", 0, 0)]),
    ]
    roles = e.classify_parts(parts)
    assert roles["U1"] == ROLE_IC
    assert roles["J1"] == ROLE_CONNECTOR
    assert roles["C1"] == ROLE_DECOUPLING_CAP
    assert roles["Y1"] == ROLE_CRYSTAL
    assert roles["R1"] == ROLE_PASSIVE
    assert roles["TP1"] == ROLE_OTHER


def test_connector_never_becomes_ic() -> None:
    """A J connector with many pads stays a connector (REQ-CLASS-002)."""
    big_conn = _part(
        "J9", "Connector:Conn_02x10",
        [_pad(str(i), i, f"N{i}", float(i), 0.0) for i in range(1, 21)],
    )
    roles = e.classify_parts([big_conn, _ic("U1")])
    assert roles["J9"] == ROLE_CONNECTOR


def test_bulk_cap_with_no_ic_on_rail_stays_passive() -> None:
    """2-pad power+GND cap whose rail reaches no IC is not a decap (REQ-DECAP-004)."""
    # +5V reaches no IC (the IC is on VCC, a different rail).
    cap = _part(
        "C9", "Capacitor_SMD:C_0805",
        [_pad("1", 10, "+5V", -0.5, 0), _pad("2", 6, "GND", 0.5, 0)],
        courtyard=(-0.5, -0.5, 0.5, 0.5),
    )
    roles = e.classify_parts([cap, _ic("U1")])
    assert roles["C9"] == ROLE_PASSIVE


# ---------------------------------------------------------------------------
# Decoupling-cap pairing
# ---------------------------------------------------------------------------

def test_pair_decaps_single_ic() -> None:
    parts = [_ic("U1"), _decap("C1"), _decap("C2")]
    roles = e.classify_parts(parts)
    pairing, warnings = e.pair_decaps(parts, roles)
    assert pairing == {"C1": "U1", "C2": "U1"}
    assert warnings == []


def test_pair_decaps_load_balanced_multi_ic() -> None:
    """3 caps over 2 ICs on the same rail → 2/1 split, lowest ref first."""
    # U2 and U10 both have a VCC pad; numeric-aware tiebreak prefers U2.
    u2 = _part("U2", "Package_SO:SOIC-8",
               [_pad("1", 11, "S0", -1, 0), _pad("2", 12, "S1", 0, 0),
                _pad("3", 13, "S2", 1, 0), _pad("8", 5, "VCC", -1, 1)],
               courtyard=(-2, -2, 2, 2))
    u10 = _part("U10", "Package_SO:SOIC-8",
                [_pad("1", 14, "T0", -1, 0), _pad("2", 15, "T1", 0, 0),
                 _pad("3", 16, "T2", 1, 0), _pad("8", 5, "VCC", -1, 1)],
                courtyard=(-2, -2, 2, 2))
    parts = [u2, u10, _decap("C1"), _decap("C2"), _decap("C3")]
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    # Load balance: C1->U2, C2->U10, C3->U2 (U2 has fewer, tiebreak lowest ref).
    counts: dict[str, int] = {}
    for ic in pairing.values():
        assert ic is not None
        counts[ic] = counts.get(ic, 0) + 1
    assert sorted(counts.values()) == [1, 2]
    assert counts["U2"] == 2 and counts["U10"] == 1


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def test_cluster_explicit_key_wins() -> None:
    """Explicit cluster_key overrides graph clustering (REQ-CLUSTER-001)."""
    a = _part("U1", "L", [_pad("1", 1, "N1", 0, 0)], cluster_key="blockA")
    b = _part("U2", "L", [_pad("1", 1, "N1", 0, 0)], cluster_key="blockB")
    # U1,U2 share net N1 (would graph-cluster) but explicit keys split them.
    parts = [a, b]
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing)
    member_sets = [set(c) for c in clusters]
    assert {"U1"} in member_sets and {"U2"} in member_sets


def test_cluster_graph_components_and_singletons() -> None:
    # U1-U2 connected by a 2-pin net (weight 1.0 >= floor); U3 isolated.
    u1 = _part("U1", "L", [_pad("1", 1, "NET", 0, 0)])
    u2 = _part("U2", "L", [_pad("1", 1, "NET", 0, 0)])
    u3 = _part("U3", "L", [_pad("1", 2, "ALONE", 0, 0)])
    parts = [u1, u2, u3]
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing)
    member_sets = [set(c) for c in clusters]
    assert {"U1", "U2"} in member_sets
    assert {"U3"} in member_sets
    # Every part lands in exactly one cluster (REQ-CLUSTER-002).
    union: set[str] = set()
    for c in clusters:
        union |= set(c)
    assert union == {"U1", "U2", "U3"}


def test_cluster_caps_travel_with_ic() -> None:
    parts = [_ic("U1"), _decap("C1")]
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing)
    for c in clusters:
        if "U1" in c:
            assert "C1" in c  # cap rides with its IC (REQ-CLUSTER-003)


def test_anchored_part_excluded_from_clusters() -> None:
    parts = [_ic("U1"), _decap("C1")]
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing, anchor_refs=frozenset({"U1"}))
    union: set[str] = set()
    for c in clusters:
        union |= set(c)
    assert "U1" not in union


# ---------------------------------------------------------------------------
# Constructive placement
# ---------------------------------------------------------------------------

def _ic_conn_decap_fixture() -> list[PartRecord]:
    return [
        _ic("U1"),
        _part("J1", "Connector:Conn_01x02",
              [_pad("1", 1, "IN0", 0, 0), _pad("2", 2, "IN1", 1, 0)]),
        _part("J2", "Connector:Conn_01x02",
              [_pad("1", 3, "OUT0", 0, 0), _pad("2", 4, "OUT1", 1, 0)]),
        _decap("C1"),
        _decap("C2"),
    ]


def test_placement_is_legal_no_overlap_in_outline() -> None:
    parts = _ic_conn_decap_fixture()
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    plan = res["plan"]
    assert _count_overlaps(parts, plan) == 0
    boxes = _boxes_for(parts, plan)
    for box in boxes.values():
        assert box[0] >= -1e-6 and box[1] >= -1e-6
        assert box[2] <= 100.0 + 1e-6 and box[3] <= 80.0 + 1e-6


def test_placement_decaps_within_max() -> None:
    parts = _ic_conn_decap_fixture()
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    dmax, dmean = e.measure_decap_distances(
        parts, res["roles"], res["decap_pairing"], res["plan"],
    )
    assert dmax is not None and dmean is not None
    assert dmax <= e.get_tunable("DECAP_MAX_MM")  # type: ignore[operator]
    # And no never-worse fallback warnings on this roomy board (AC2).
    assert not any(w["type"] == "decap_fallback" for w in res["warnings"])


def test_placement_deterministic() -> None:
    parts = _ic_conn_decap_fixture()
    r1 = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    r2 = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    assert r1["plan"] == r2["plan"]
    assert r1["warnings"] == r2["warnings"]


def test_anchors_preserved_and_excluded_from_plan() -> None:
    """Anchored refs never appear in the plan (caller leaves them put, AC7)."""
    parts = _ic_conn_decap_fixture()
    anchors = {"J1": (90.0, 40.0, 90.0)}
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5, anchors=anchors)
    assert "J1" not in res["plan"]
    for ref in ("U1", "J2", "C1", "C2"):
        assert ref in res["plan"]


def test_net_aware_beats_naive_spread() -> None:
    """Net-aware HPWL is far below a naive wide spread of the same netlist."""
    parts = _ic_conn_decap_fixture()
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    h_net = e._total_hpwl_of(parts, res["plan"], {})
    # A deliberately spread layout of the same parts.
    spread = {
        "U1": (10.0, 10.0, 0.0), "J1": (90.0, 10.0, 0.0),
        "J2": (10.0, 70.0, 0.0), "C1": (90.0, 70.0, 0.0),
        "C2": (50.0, 40.0, 0.0),
    }
    h_spread = e._total_hpwl_of(parts, spread, {})
    assert h_net < h_spread


# ---------------------------------------------------------------------------
# Legalization
# ---------------------------------------------------------------------------

def test_legalize_separates_overlap() -> None:
    parts = [
        _part("U1", "L", [], (-2, -2, 2, 2)),
        _part("U2", "L", [], (-2, -2, 2, 2)),
    ]
    plan = {"U1": (5.0, 5.0, 0.0), "U2": (6.0, 5.0, 0.0)}
    new_plan, warnings = e.legalize(parts, plan, {}, (0.0, 0.0, 50.0, 50.0), 0.5)
    assert _count_overlaps(parts, new_plan) == 0
    assert warnings == []


def test_legalize_incomplete_terminates_on_impossible() -> None:
    """Two board-sized parts can't separate → structured warning, no infinite loop."""
    parts = [
        _part("A", "L", [], (-2, -2, 2, 2)),
        _part("B", "L", [], (-2, -2, 2, 2)),
    ]
    plan = {"A": (2.0, 2.0, 0.0), "B": (2.0, 2.0, 0.0)}
    new_plan, warnings = e.legalize(parts, plan, {}, (0.0, 0.0, 4.0, 4.0), 0.5)
    assert any(w["type"] == "legalize_incomplete" for w in warnings)


# ---------------------------------------------------------------------------
# Board-text parser
# ---------------------------------------------------------------------------

def test_read_part_records_parses_geometry_nets_cluster() -> None:
    board = (
        '(kicad_pcb\n'
        '\t(net 0 "")\n'
        '\t(net 1 "VCC")\n'
        '\t(net 2 "GND")\n'
        '\t(footprint "Capacitor_SMD:C_0402"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(at 10 20 90)\n'
        '\t\t(property "Reference" "C1" (at 0 0 0))\n'
        '\t\t(property "ClusterId" "rf")\n'
        '\t\t(fp_rect (start -0.5 -0.5) (end 0.5 0.5) (layer "F.CrtYd"))\n'
        '\t\t(pad "1" smd roundrect (at -0.5 0) (size 0.3 0.3) (layers "F.Cu") (net 1 "VCC"))\n'
        '\t\t(pad "2" smd roundrect (at 0.5 0) (size 0.3 0.3) (layers "F.Cu") (net 2 "GND"))\n'
        '\t)\n'
        ')\n'
    )
    recs = e.read_part_records(board)
    assert len(recs) == 1
    r = recs[0]
    assert r["ref"] == "C1"
    assert r["lib_id"] == "Capacitor_SMD:C_0402"
    assert r["cluster_key"] == "rf"
    assert r["pad_count"] == 2
    assert r["courtyard"] == (-0.5, -0.5, 0.5, 0.5)
    assert r["pos"] == (10.0, 20.0, 90.0)
    nets = {p["pad"]: p["net_name"] for p in r["pads"]}
    assert nets == {"1": "VCC", "2": "GND"}


def test_read_part_records_skips_pseudo_refs() -> None:
    board = (
        '(kicad_pcb\n'
        '\t(footprint "x" (at 0 0)\n'
        '\t\t(property "Reference" "#PWR01" (at 0 0 0))\n'
        '\t)\n'
        '\t(footprint "y" (at 5 5)\n'
        '\t\t(property "Reference" "R1" (at 0 0 0))\n'
        '\t)\n'
        ')\n'
    )
    recs = e.read_part_records(board)
    assert [r["ref"] for r in recs] == ["R1"]


# ---------------------------------------------------------------------------
# P3 — orientation normalization (REQ-ORIENT-001..004)
# ---------------------------------------------------------------------------

_RES_LIB = "Resistor_SMD:R_0402"


def _family_consistency(
    parts: list[PartRecord], plan: dict[str, tuple[float, float, float]],
) -> float:
    """Local mirror of placement_metrics._orientation_consistency over a plan."""
    by_ref = {p["ref"]: p for p in parts}
    families: dict[str, list[float]] = {}
    for ref, (_x, _y, rot) in plan.items():
        families.setdefault(by_ref[ref]["lib_id"], []).append(rot)
    weighted = 0
    total = 0
    for rots in families.values():
        if len(rots) < 2:
            continue
        buckets: dict[float, int] = {}
        for r in rots:
            q = round((r % 360.0) / 90.0) * 90.0 % 360.0
            buckets[q] = buckets.get(q, 0) + 1
        weighted += max(buckets.values())
        total += len(rots)
    return 1.0 if total == 0 else weighted / total


def _iso_res(ref: str, x: float, rot: float) -> PartRecord:
    """A resistor on unique (isolated) nets → zero HPWL contribution."""
    return _part(
        ref, _RES_LIB,
        [_pad("1", hash(ref) % 9000 + 100, ref + "A", -0.5, 0),
         _pad("2", hash(ref) % 9000 + 101, ref + "B", 0.5, 0)],
        pos=(x, 10.0, rot),
    )


def _vertical_res(ref: str, rot: float) -> tuple[
    list[PartRecord], dict[str, tuple[float, float, float]],
]:
    """A resistor whose two nets pull vertically: rot=90 is strictly lower HPWL.

    Returns ``([r], anchors)`` — two connector anchors above/below hold the far
    ends of the resistor's two signal nets, so a horizontal (rot 0) placement is
    longer than a vertical (rot 90) one.
    """
    r = _part(
        ref, _RES_LIB,
        [_pad("1", 200, "NA", -0.5, 0), _pad("2", 201, "NB", 0.5, 0)],
        pos=(50.0, 20.0, rot),
    )
    top = _part("J8", "Connector:Conn", [_pad("1", 200, "NA", 0, 0)])
    bot = _part("J9", "Connector:Conn", [_pad("1", 201, "NB", 0, 0)])
    anchors = {"J8": (50.0, 5.0, 0.0), "J9": (50.0, 35.0, 0.0)}
    return [r, top, bot], anchors


def test_orientation_normalization_improves_consistency() -> None:
    """(b) snaps an off-modal family member to the mode, HPWL-neutral (REQ-ORIENT
    -001/004): consistency strictly increases, no new overlap, HPWL not raised."""
    parts = [_iso_res("R1", 10.0, 90.0), _iso_res("R2", 20.0, 90.0),
             _iso_res("R3", 30.0, 0.0)]
    plan = {p["ref"]: p["pos"] for p in parts}
    roles = e.classify_parts(parts)
    board = (0.0, 0.0, 100.0, 50.0)

    before = _family_consistency(parts, plan)
    h_before = e._total_hpwl_of(parts, plan, {})
    new_plan, _w = e.normalize_orientations(parts, plan, roles, {}, board)
    after = _family_consistency(parts, new_plan)

    assert after > before          # 2/3 → 1.0 (REQ-ORIENT-004)
    assert after == 1.0
    assert new_plan["R3"][2] == 90.0
    assert e._total_hpwl_of(parts, new_plan, {}) <= h_before + 1e-9
    assert _count_overlaps(parts, new_plan) == 0


def test_orientation_rotation_for_hpwl() -> None:
    """(a) accepts a quantized rotation on a strict HPWL decrease (REQ-ORIENT-001)."""
    parts, anchors = _vertical_res("R7", 0.0)
    plan = {"R7": (50.0, 20.0, 0.0)}
    roles = e.classify_parts(parts)
    board = (0.0, 0.0, 100.0, 50.0)

    h_before = e._total_hpwl_of(parts, plan, anchors)
    new_plan, _w = e.normalize_orientations(parts, plan, roles, anchors, board)

    assert new_plan["R7"][2] == 90.0  # rotated to the low-HPWL orientation
    assert e._total_hpwl_of(parts, new_plan, anchors) < h_before


def test_orientation_never_worse_rejects_snap() -> None:
    """A snap to the family mode that would raise HPWL is rejected (REQ-ORIENT-004
    never-worse): the HPWL-optimal off-modal member keeps its rotation."""
    vparts, anchors = _vertical_res("R3", 90.0)   # R3 optimal at 90
    parts = [_iso_res("R1", 10.0, 0.0), _iso_res("R2", 20.0, 0.0)] + vparts
    plan = {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0),
            "R3": (50.0, 20.0, 90.0)}
    roles = e.classify_parts(parts)
    board = (0.0, 0.0, 100.0, 50.0)

    new_plan, _w = e.normalize_orientations(parts, plan, roles, anchors, board)

    # modal is 0 (R1,R2) but snapping R3 to 0 raises HPWL → R3 stays at 90.
    assert new_plan["R3"][2] == 90.0
    assert new_plan["R1"][2] == 0.0 and new_plan["R2"][2] == 0.0


def test_orientation_leaves_connectors_and_anchors(  # REQ-ORIENT-002
) -> None:
    parts, anchors = _vertical_res("R7", 0.0)
    plan = {"R7": (50.0, 20.0, 0.0)}
    roles = e.classify_parts(parts)
    board = (0.0, 0.0, 100.0, 50.0)
    new_plan, _w = e.normalize_orientations(parts, plan, roles, anchors, board)
    # Connectors are anchors here — never in the plan, never rotated.
    assert "J8" not in new_plan and "J9" not in new_plan


# ---------------------------------------------------------------------------
# P3 — signal-flow cluster ordering (REQ-FLOW-001..003)
# ---------------------------------------------------------------------------

def _flow_fixture() -> tuple[
    list[PartRecord], dict[str, tuple[float, float, float]],
]:
    """Input connector (USB) at the left edge, output (SPK) at the right edge.

    ``R20``'s cluster has the larger courtyard, so the P2 area order would place
    it first; flow ordering must override that and place the input-biased ``R10``
    first.
    """
    r10 = _part("R10", "R_x",
                [_pad("1", 10, "SIG_IN", -0.5, 0), _pad("2", 11, "NA", 0.5, 0)])
    r20 = _part("R20", "R_y",
                [_pad("1", 12, "SIG_OUT", -0.5, 0), _pad("2", 13, "NB", 0.5, 0)],
                courtyard=(-3, -3, 3, 3))
    j1 = _part("J1", "Connector:USB",
               [_pad("1", 10, "SIG_IN", 0, 0), _pad("2", 14, "USB_DP", 0, 1)])
    j2 = _part("J2", "Connector:Audio",
               [_pad("1", 12, "SIG_OUT", 0, 0), _pad("2", 15, "SPK_OUT", 0, 1)])
    anchors = {"J1": (5.0, 25.0, 0.0), "J2": (95.0, 25.0, 0.0)}
    return [r10, r20, j1, j2], anchors


def test_flow_orders_input_to_output() -> None:
    parts, anchors = _flow_fixture()
    board = (0.0, 0.0, 100.0, 50.0)
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing, frozenset(anchors))
    graph = e._part_graph(parts)

    order = e.order_clusters_by_flow(
        clusters, parts, roles, anchors, graph, board,
    )
    assert order is not None
    # Input-biased cluster first, despite R20's larger courtyard (area order
    # would be the reverse) — REQ-FLOW-001.
    assert order[0] == ["R10"]
    assert order[1] == ["R20"]

    # And the placed centroids run input(left) → output(right) along x.
    res = e.plan_placement(parts, board, 0.5, anchors=anchors)
    assert res["plan"]["R10"][0] < res["plan"]["R20"][0]


def test_flow_graceful_degradation_single_connector() -> None:
    """<2 distinguishable endpoints → order_clusters_by_flow is a no-op (None),
    so plan_placement keeps the exact P2 area order (REQ-FLOW-002)."""
    parts, anchors = _flow_fixture()
    anchors = {"J1": (5.0, 25.0, 0.0)}   # drop the output connector
    board = (0.0, 0.0, 100.0, 50.0)
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing, frozenset(anchors))
    graph = e._part_graph(parts)

    assert e.order_clusters_by_flow(
        clusters, parts, roles, anchors, graph, board,
    ) is None
    # Still produces a legal, overlap-free layout.
    res = e.plan_placement(parts, board, 0.5, anchors=anchors)
    assert _count_overlaps(parts, res["plan"]) == 0


def test_p3_pipeline_deterministic() -> None:
    """Two full runs with flow ordering + orientation active are identical."""
    parts, anchors = _flow_fixture()
    board = (0.0, 0.0, 100.0, 50.0)
    r1 = e.plan_placement(parts, board, 0.5, anchors=anchors)
    r2 = e.plan_placement(parts, board, 0.5, anchors=anchors)
    assert r1["plan"] == r2["plan"]
    assert r1["warnings"] == r2["warnings"]


def test_place_default_order_unchanged_without_cluster_order() -> None:
    """place() called without cluster_order keeps P2 area behavior (REQ-BACK)."""
    parts = _ic_conn_decap_fixture()
    roles = e.classify_parts(parts)
    pairing, _ = e.pair_decaps(parts, roles)
    clusters = e.cluster_parts(parts, roles, pairing)
    board = (0.0, 0.0, 100.0, 80.0)
    p_default, _w1 = e.place(parts, roles, clusters, pairing, {}, board, 0.5)
    area = e._order_clusters_by_area(clusters, {p["ref"]: p for p in parts})
    p_explicit, _w2 = e.place(
        parts, roles, clusters, pairing, {}, board, 0.5, cluster_order=area,
    )
    assert p_default == p_explicit


# ---------------------------------------------------------------------------
# P4 — sensitive-net proximity (REQ-SENSE-001..003, REQ-TEST-P4-001)
# ---------------------------------------------------------------------------

import math  # noqa: E402 — grouped with the P4 helpers that need it

from kicad_mcp.utils.placement_config import is_clock_net  # noqa: E402


def _xtal_ic(ref: str = "U1") -> PartRecord:
    """An IC whose oscillator (XIN/XOUT) pads sit on its east side (x = +1)."""
    return _part(
        ref, "Package_QFP:LQFP-32",
        [
            _pad("1", 1, "IN0", -1, -0.5), _pad("2", 2, "IN1", -1, 0.5),
            _pad("5", 5, "VCC", -1, 1), _pad("4", 6, "GND", 0, 1),
            _pad("7", 20, "XIN", 1, -0.5), _pad("8", 21, "XOUT", 1, 0.5),
        ],
        courtyard=(-2, -2, 2, 2),
    )


def _crystal(
    ref: str = "Y1",
    courtyard: tuple[float, float, float, float] = (-0.9, -0.9, 0.9, 0.9),
) -> PartRecord:
    return _part(
        ref, "Crystal:Crystal_SMD_3225-4Pin",
        [_pad("1", 20, "XIN", -0.6, 0), _pad("2", 21, "XOUT", 0.6, 0)],
        courtyard=courtyard,
    )


def _load_cap(ref: str, net: str, nid: int) -> PartRecord:
    """A crystal load cap: oscillator net to GND (never a decap — no rail)."""
    return _part(
        ref, "Capacitor_SMD:C_0402",
        [_pad("1", nid, net, -0.5, 0), _pad("2", 6, "GND", 0.5, 0)],
        courtyard=(-0.5, -0.5, 0.5, 0.5),
    )


def _crystal_fixture() -> list[PartRecord]:
    return [
        _xtal_ic("U1"), _crystal("Y1"),
        _load_cap("C10", "XIN", 20), _load_cap("C11", "XOUT", 21),
        _decap("C1"),
    ]


def _centre_dist(
    parts: list[PartRecord],
    pos_of: dict[str, tuple[float, float, float]],
    a: str, b: str,
) -> float:
    by_ref = {p["ref"]: p for p in parts}

    def centre(r: str) -> tuple[float, float]:
        box = e._board_box(by_ref[r], (pos_of[r][0], pos_of[r][1]), pos_of[r][2])
        return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

    (ax, ay), (bx, by) = centre(a), centre(b)
    return math.hypot(ax - bx, ay - by)


def test_crystal_classification_and_pairing() -> None:
    """Crystal pairs to the IC it shares the most signal weight with; a crystal
    sharing nothing with any IC maps to None (REQ-SENSE-001)."""
    parts = _crystal_fixture()
    roles = e.classify_parts(parts)
    assert roles["Y1"] == ROLE_CRYSTAL
    assert roles["C10"] == ROLE_PASSIVE and roles["C11"] == ROLE_PASSIVE
    graph = e._part_graph(parts)
    assert e.pair_crystals(parts, roles, graph) == {"Y1": "U1"}

    lonely = _part("Y9", "Crystal:Crystal_SMD",
                   [_pad("1", 90, "FLOAT_A", 0, 0), _pad("2", 91, "FLOAT_B", 1, 0)])
    parts2 = [_xtal_ic("U1"), lonely]
    roles2 = e.classify_parts(parts2)
    assert e.pair_crystals(parts2, roles2, e._part_graph(parts2)) == {"Y9": None}


def test_crystal_load_caps_found_decap_excluded() -> None:
    """Load caps = 2-pad C parts sharing a crystal net; decaps never counted."""
    parts = _crystal_fixture()
    roles = e.classify_parts(parts)
    pairing = e.pair_crystals(parts, roles, e._part_graph(parts))
    assert e.crystal_load_caps(parts, roles, pairing) == {"Y1": ["C10", "C11"]}


def test_crystal_hugs_ic_with_load_caps() -> None:
    """Crystal lands within SENSE_MAX_MM of its IC, load caps within reach of the
    crystal, no fallback warnings, plan stays legal (REQ-SENSE-001)."""
    parts = _crystal_fixture()
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    plan = res["plan"]
    sense_max = e.get_float("SENSE_MAX_MM")
    assert _centre_dist(parts, plan, "Y1", "U1") <= sense_max
    assert _centre_dist(parts, plan, "C10", "Y1") <= sense_max
    assert _centre_dist(parts, plan, "C11", "Y1") <= sense_max
    assert not any(w["type"] == "sense_fallback" for w in res["warnings"])
    assert _count_overlaps(parts, plan) == 0


def test_crystal_prefers_oscillator_pin_side() -> None:
    """With all four sides free and equidistant (square crystal courtyard), the
    crystal takes the side holding the IC's oscillator pads — east here."""
    parts = _crystal_fixture()
    res = e.plan_placement(parts, (0.0, 0.0, 100.0, 80.0), 0.5)
    plan = res["plan"]
    by_ref = {p["ref"]: p for p in parts}
    u1_box = e._board_box(by_ref["U1"], (plan["U1"][0], plan["U1"][1]), plan["U1"][2])
    y1_box = e._board_box(by_ref["Y1"], (plan["Y1"][0], plan["Y1"][1]), plan["Y1"][2])
    assert (y1_box[0] + y1_box[2]) / 2.0 > (u1_box[0] + u1_box[2]) / 2.0


def test_crystal_never_worse_fallback_warning() -> None:
    """No legal adjacent slot (board barely larger than the IC) → structured
    sense_fallback warning; the crystal is still placed (REQ-SENSE-001)."""
    parts = [_xtal_ic("U1"), _crystal("Y1")]
    res = e.plan_placement(parts, (0.0, 0.0, 5.0, 5.0), 0.1)
    assert any(w["type"] == "sense_fallback" for w in res["warnings"])
    assert "Y1" in res["plan"]


def test_diff_pair_weight_multiplier_in_graph() -> None:
    """A declared diff-pair net's per-pair weight scales by DIFFPAIR_WEIGHT_MULT
    (REQ-SENSE-002); undeclared, the graph is unchanged P1 weighting."""
    a = _part("U1", "L", [_pad("1", 30, "PAIR_P", 0, 0),
                          _pad("2", 31, "PAIR_N", 0.5, 0)])
    b = _part("U2", "L", [_pad("1", 30, "PAIR_P", 0, 0),
                          _pad("2", 31, "PAIR_N", 0.5, 0)])
    key = frozenset(("U1", "U2"))
    base = e._part_graph([a, b])[key]
    boosted = e._part_graph([a, b], frozenset({"PAIR_P", "PAIR_N"}))[key]
    assert boosted == base * e.get_float("DIFFPAIR_WEIGHT_MULT")


def test_diff_pair_binds_cluster() -> None:
    """A net too weak to cluster on its own (fanout 6 → 0.2 < floor 0.25) binds
    its parts once declared as a diff pair (0.8) — the boost steers clustering,
    not just placement (REQ-SENSE-002)."""
    parts = [
        _part(f"U{i}", "L", [_pad("1", 30, "PAIR_P", 0, 0)]) for i in range(1, 7)
    ]
    roles = e.classify_parts(parts)
    plain = e.cluster_parts(parts, roles, {})
    assert all(len(c) == 1 for c in plain)
    declared = e.cluster_parts(
        parts, roles, {}, frozenset(), frozenset({"PAIR_P"}),
    )
    assert [len(c) for c in declared] == [6]


def test_clock_net_intermediate_weight() -> None:
    """A clock-like net's per-pair weight is 1/(m-1) * CLOCK_WEIGHT_MULT —
    between a plain signal (x1) and a bus (x0) (REQ-SENSE-003)."""
    mult = e.get_float("CLOCK_WEIGHT_MULT")
    a = _part("U1", "L", [_pad("1", 50, "SPI_CLK", 0, 0)])
    b = _part("U2", "L", [_pad("1", 50, "SPI_CLK", 0, 0)])
    assert e._part_graph([a, b])[frozenset(("U1", "U2"))] == 1.0 * mult

    three = [
        _part(f"U{i}", "L", [_pad("1", 51, "SYS_CLK", 0, 0)]) for i in range(1, 4)
    ]
    g = e._part_graph(three)
    assert g[frozenset(("U1", "U2"))] == 0.5 * mult


def test_is_clock_net_pattern() -> None:
    for name in ("CLK", "SYS_CLK", "CLKOUT", "XTAL1", "OSC_IN", "MCU_XTAL_IN"):
        assert is_clock_net(name), name
    for name in ("MISO", "VCC", "BLINK", "MYCLK", ""):
        assert not is_clock_net(name), name


def test_diff_pair_wins_over_clock() -> None:
    """A net that is both declared diff pair and clock-like takes the diff-pair
    multiplier (REQ-SENSE-002 over -003)."""
    mults = e._net_weight_multipliers(
        frozenset({"USB_CLK", "PLAIN"}), frozenset({"USB_CLK"}),
    )
    assert mults == {"USB_CLK": e.get_float("DIFFPAIR_WEIGHT_MULT")}


def test_read_diff_pair_nets(tmp_path) -> None:
    """Diff-pair nets come from .kicad_pro netclass data: exact patterns verbatim,
    globs expanded against board nets, Default class ignored (REQ-SENSE-002)."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (net 1 "USB_DP") (net 2 "USB_DM") (net 3 "SIG_A"))',
        encoding="utf-8",
    )
    pro = tmp_path / "board.kicad_pro"

    import json
    pro.write_text(json.dumps({
        "net_settings": {
            "classes": [
                {"name": "Default", "diff_pair_width": 0.2},
                {"name": "USB90", "diff_pair_width": 0.2, "diff_pair_gap": 0.15},
            ],
            "netclass_patterns": [
                {"netclass": "USB90", "pattern": "USB_D*"},
                {"netclass": "USB90", "pattern": "SIG_A"},
            ],
        },
    }), encoding="utf-8")
    assert e.read_diff_pair_nets(pcb) == frozenset({"USB_DP", "USB_DM", "SIG_A"})

    # A class with no positive diff_pair_* field declares nothing.
    pro.write_text(json.dumps({
        "net_settings": {
            "classes": [{"name": "USB90", "clearance": 0.2}],
            "netclass_patterns": [{"netclass": "USB90", "pattern": "USB_D*"}],
        },
    }), encoding="utf-8")
    assert e.read_diff_pair_nets(pcb) == frozenset()

    pro.unlink()
    assert e.read_diff_pair_nets(pcb) == frozenset()


def test_p4_pipeline_deterministic() -> None:
    """Two runs with sensitive placement + diff-pair weighting are identical."""
    parts = _crystal_fixture()
    board = (0.0, 0.0, 100.0, 80.0)
    dp = frozenset({"XIN", "XOUT"})
    r1 = e.plan_placement(parts, board, 0.5, diff_pair_nets=dp)
    r2 = e.plan_placement(parts, board, 0.5, diff_pair_nets=dp)
    assert r1["plan"] == r2["plan"]
    assert r1["warnings"] == r2["warnings"]
