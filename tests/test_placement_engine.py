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
