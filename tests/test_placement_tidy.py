"""FC (KIR3) — post-legalization placement tidy-up pass.

tidy_placement snaps parts to a coarse grid, aligns same-footprint rows, squares
paired parts to a shared orientation, and tightens far decaps — each move guarded
legal + Total-HPWL-non-increasing. Anchors are never moved; the pass is
deterministic and a no-op when disabled.
"""

from __future__ import annotations

import math

from kicad_mcp.utils import placement_config as pc
from kicad_mcp.utils import placement_engine as pe
from kicad_mcp.utils.placement_engine import (
    ROLE_DECOUPLING_CAP,
    ROLE_IC,
    ROLE_PASSIVE,
    PadLocal,
    PartRecord,
)

BOARD = (0.0, 0.0, 60.0, 60.0)


def _pad(name: str, nid: int, net: str, dx: float, dy: float) -> PadLocal:
    return PadLocal(pad=name, net_id=nid, net_name=net, dx=dx, dy=dy)


def _part(ref, lib, pads, courtyard=(-1.0, -1.0, 1.0, 1.0), pos=(0.0, 0.0, 0.0)) -> PartRecord:
    return PartRecord(
        ref=ref, lib_id=lib, cluster_key="", pad_count=len(pads),
        courtyard=courtyard, pads=pads, pos=pos,
    )


def _r(ref: str, na: int, nb: int) -> PartRecord:
    return _part(
        ref, "Resistor_SMD:R_0402",
        [_pad("1", na, f"N{na}", -0.5, 0), _pad("2", nb, f"N{nb}", 0.5, 0)],
        courtyard=(-0.5, -0.5, 0.5, 0.5),
    )


def _overlaps(parts, plan) -> int:
    by_ref = {p["ref"]: p for p in parts}
    refs = sorted(plan)
    n = 0
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            a = pe._board_box(by_ref[refs[i]], plan[refs[i]][:2], plan[refs[i]][2])
            b = pe._board_box(by_ref[refs[j]], plan[refs[j]][:2], plan[refs[j]][2])
            if pe._boxes_overlap(a, b, gap=0.0):
                n += 1
    return n


# ── steps 1–3 ────────────────────────────────────────────────────────────────

def test_tidy_snaps_to_grid():
    parts = [_r("R1", 1, 2), _r("R2", 3, 4)]  # distinct nets → 0 HPWL
    plan = {"R1": (10.03, 9.98, 0.0), "R2": (20.02, 15.04, 0.0)}
    roles = {"R1": ROLE_PASSIVE, "R2": ROLE_PASSIVE}
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    for r in ("R1", "R2"):
        x, y, _rot = out[r]
        assert abs(x / 0.5 - round(x / 0.5)) < 1e-6
        assert abs(y / 0.5 - round(y / 0.5)) < 1e-6
    assert out["R1"] == (10.0, 10.0, 0.0)


def test_tidy_aligns_same_footprint_row():
    parts = [_r("R1", 1, 2), _r("R2", 3, 4), _r("R3", 5, 6)]
    plan = {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.3, 0.0), "R3": (30.0, 9.8, 0.0)}
    roles = {r: ROLE_PASSIVE for r in ("R1", "R2", "R3")}
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    ys = {round(out[r][1], 3) for r in ("R1", "R2", "R3")}
    assert len(ys) == 1  # all pulled onto one shared line


def test_tidy_squares_pair_rotation():
    parts = [_r("R1", 1, 2), _r("R2", 3, 4)]
    plan = {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 90.0)}
    roles = {"R1": ROLE_PASSIVE, "R2": ROLE_PASSIVE}
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    assert pe._quantize_rot(out["R1"][2], 90.0) == pe._quantize_rot(out["R2"][2], 90.0)


# ── guards: HPWL, overlaps, anchors, determinism ──────────────────────────────

def _messy():
    u1 = _part(
        "U1", "Package_SO:SOIC-8",
        [_pad("1", 1, "N1", -1, 0), _pad("2", 2, "N2", 1, 0),
         _pad("3", 3, "GND", 0, -1), _pad("4", 4, "N3", 0, 1)],
        courtyard=(-2, -2, 2, 2),
    )
    r1 = _part("R1", "Resistor_SMD:R_0402",
               [_pad("1", 1, "N1", -0.5, 0), _pad("2", 5, "N5", 0.5, 0)],
               courtyard=(-0.5, -0.5, 0.5, 0.5))
    r2 = _part("R2", "Resistor_SMD:R_0402",
               [_pad("1", 2, "N2", -0.5, 0), _pad("2", 6, "N6", 0.5, 0)],
               courtyard=(-0.5, -0.5, 0.5, 0.5))
    parts = [u1, r1, r2]
    plan = {"U1": (25.02, 25.03, 0.0), "R1": (30.04, 25.98, 0.0), "R2": (19.97, 25.01, 90.0)}
    roles = {"U1": ROLE_IC, "R1": ROLE_PASSIVE, "R2": ROLE_PASSIVE}
    return parts, plan, roles


def test_tidy_hpwl_never_increases():
    parts, plan, roles = _messy()
    before = pe._total_hpwl_of(parts, plan, {})
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    assert pe._total_hpwl_of(parts, out, {}) <= before + 1e-6


def test_tidy_no_new_overlap():
    parts, plan, roles = _messy()
    assert _overlaps(parts, plan) == 0  # fixture starts clean
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    assert _overlaps(parts, out) == 0


def test_tidy_never_moves_anchor():
    parts, plan, roles = _messy()
    j1 = _part("J1", "Connector:Conn_01x02",
               [_pad("1", 9, "N9", 0, 0), _pad("2", 10, "N10", 1, 0)],
               courtyard=(-1, -1, 1, 1))
    parts = parts + [j1]  # J1 is a real footprint, held fixed as an anchor
    anchors = {"J1": (5.0, 5.0, 0.0)}
    out, _ = pe.tidy_placement(parts, plan, roles, anchors, BOARD)
    assert "J1" not in out  # anchors are fixed obstacles, never in the plan
    assert _overlaps(parts, {**out, **anchors}) == 0  # nothing moved onto the anchor


def test_tidy_deterministic():
    parts, plan, roles = _messy()
    o1, _ = pe.tidy_placement(parts, dict(plan), roles, {}, BOARD)
    o2, _ = pe.tidy_placement(parts, dict(plan), roles, {}, BOARD)
    assert o1 == o2


def test_tidy_disabled_is_noop(monkeypatch):
    parts, plan, roles = _messy()
    monkeypatch.setattr(pc, "TIDY_ENABLE", False)
    out, warns = pe.tidy_placement(parts, dict(plan), roles, {}, BOARD)
    assert out == plan
    assert warns == []


# ── step 4: decap tightening ──────────────────────────────────────────────────

def _ic_cap(ic_court, cap_pos):
    u1 = _part(
        "U1", "IC",
        [_pad("1", 1, "VCC", 0, 0), _pad("2", 2, "GND", 1, 0),
         _pad("3", 3, "A", -1, 0), _pad("4", 4, "B", 0, 1)],
        courtyard=ic_court,
    )
    c1 = _part("C1", "Capacitor_SMD:C_0402",
               [_pad("1", 1, "VCC", -0.5, 0), _pad("2", 2, "GND", 0.5, 0)],
               courtyard=(-0.5, -0.5, 0.5, 0.5))
    parts = [u1, c1]
    plan = {"U1": (25.0, 25.0, 0.0), "C1": cap_pos}
    roles = {"U1": ROLE_IC, "C1": ROLE_DECOUPLING_CAP}
    return parts, plan, roles


def test_tidy_decap_tightens_when_slot_free():
    parts, plan, roles = _ic_cap((-2, -2, 2, 2), (45.0, 45.0, 0.0))  # ~28 mm away
    out, warns = pe.tidy_placement(parts, plan, roles, {}, BOARD, decap_pairing={"C1": "U1"})
    d = math.hypot(out["C1"][0] - 25.0, out["C1"][1] - 25.0)
    assert d < 10.0  # pulled in close to the IC
    assert not any(w.get("type") == "decap_tidy_deferred" for w in warns)


def test_tidy_decap_skips_when_already_close():
    parts, plan, roles = _ic_cap((-1, -1, 1, 1), (27.0, 25.0, 0.0))  # ~2 mm, ≤ target
    out, warns = pe.tidy_placement(parts, plan, roles, {}, BOARD, decap_pairing={"C1": "U1"})
    assert out["C1"] == (27.0, 25.0, 0.0)  # already close → step 4 leaves it
    assert not any(w.get("type") == "decap_tidy_deferred" for w in warns)


def test_tidy_decap_defers_when_no_closer_slot():
    # Big IC (half 5) → the cap's closest legal spot is 5.5 mm, still > 3 mm target,
    # and no legal position is nearer, so the pass reports an advisory and moves nothing.
    parts, plan, roles = _ic_cap((-5, -5, 5, 5), (30.5, 25.0, 0.0))
    out, warns = pe.tidy_placement(parts, plan, roles, {}, BOARD, decap_pairing={"C1": "U1"})
    assert out["C1"] == (30.5, 25.0, 0.0)  # unchanged
    assert any(
        w.get("type") == "decap_tidy_deferred" and w.get("cap") == "C1" for w in warns
    )


def test_tidy_decap_distance_never_increases():
    # C1 sits 2.9 mm to the left of an off-grid IC (≤ target). Grid-snapping the
    # IC would move it AWAY from the cap (→ 3.1 mm); the decap guard must reject
    # that move, so the cap distance never regresses.
    parts, plan, roles = _ic_cap((-2, -2, 2, 2), (22.4, 25.0, 0.0))
    plan["U1"] = (25.3, 25.0, 0.0)
    pairing = {"C1": "U1"}
    before = pe.measure_decap_distances(parts, roles, pairing, plan)[0]
    out, _ = pe.tidy_placement(parts, plan, roles, {}, BOARD, decap_pairing=pairing)
    after = pe.measure_decap_distances(parts, roles, pairing, out)[0]
    assert after is not None and before is not None and after <= before + 1e-6
    assert out["U1"] == (25.3, 25.0, 0.0)  # the worsening IC snap was rejected


def test_tidy_summary_reported():
    parts, plan, roles = _messy()
    _out, warns = pe.tidy_placement(parts, plan, roles, {}, BOARD)
    summary = [w for w in warns if w.get("type") == "tidy_summary"]
    assert len(summary) == 1
    assert summary[0]["hpwl_after"] <= summary[0]["hpwl_before"] + 1e-6
