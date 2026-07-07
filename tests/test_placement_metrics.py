"""REQ-TEST-P1-001 — unit tests for the placement-quality metric (Sprint P1).

Boards are tiny, hand-authored ``.kicad_pcb`` strings with known pad coordinates
written to ``tmp_path`` — no KiCad installation required (matches the tmp-project
convention used by the design-rules tests).
"""

from __future__ import annotations

import math
from pathlib import Path

from kicad_mcp.utils.placement_config import classify_net
from kicad_mcp.utils.placement_metrics import (
    build_net_pads,
    build_part_graph,
    placement_metric,
    read_board_pads,
)

# ---------------------------------------------------------------------------
# Minimal board builder
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
\t)
"""


def _pad(name: str, px: float, py: float, nid: int, nname: str) -> str:
    return (
        f'\t\t(pad "{name}" smd roundrect (at {px} {py}) (size 1 1) '
        f'(layers "F.Cu" "F.Mask" "F.Paste") (net {nid} "{nname}"))\n'
    )


def _footprint(
    lib_id: str,
    ref: str,
    at: tuple[float, float, float],
    pads: list[tuple[str, float, float, int, str]],
    courtyard: tuple[float, float, float, float] | None = (-2.0, -2.0, 2.0, 2.0),
) -> str:
    fx, fy, frot = at
    s = f'\t(footprint "{lib_id}"\n'
    s += '\t\t(layer "F.Cu")\n'
    s += f"\t\t(at {fx} {fy} {frot})\n"
    s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    s += f'\t\t(property "Value" "{ref}_val" (at 0 0 0) (layer "F.Fab"))\n'
    if courtyard is not None:
        cx0, cy0, cx1, cy1 = courtyard
        s += (
            f"\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) "
            '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        )
    for pad in pads:
        s += _pad(*pad)
    s += "\t)\n"
    return s


def _board(
    footprints: list[str],
    nets: list[tuple[int, str]],
    outline: tuple[float, float, float, float] | None = (0.0, 0.0, 60.0, 60.0),
) -> str:
    s = _HEADER
    s += '\t(net 0 "")\n'
    for nid, nname in nets:
        s += f'\t(net {nid} "{nname}")\n'
    if outline is not None:
        x0, y0, x1, y1 = outline
        s += (
            f"\t(gr_rect (start {x0} {y0}) (end {x1} {y1}) "
            '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
        )
    for fp in footprints:
        s += fp
    s += ")\n"
    return s


def _write(tmp_path: Path, content: str, name: str = "b.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_hand_computed_hpwl(tmp_path: Path) -> None:
    """Two footprints, one 2-pin signal net at known coords -> hand HPWL."""
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")],
                   courtyard=(-2.0, -2.0, 2.0, 2.0)),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")],
                   courtyard=(-2.0, -2.0, 2.0, 2.0)),
    ]
    board = _board(fps, [(1, "SIG")])
    p = _write(tmp_path, board)

    # HPWL = (30-10) + (10-10) = 20
    bundle = placement_metric(p)
    assert bundle["total_hpwl_mm"] == 20.0
    assert bundle["signal_net_count"] == 1
    assert bundle["scored_parts"] == 2
    assert bundle["overlap_count"] == 0
    assert bundle["out_of_outline_count"] == 0
    assert bundle["decap_max_mm"] is None
    assert bundle["decap_mean_mm"] is None


def test_power_net_excluded(tmp_path: Path) -> None:
    """Adding a GND net spanning both parts must not change HPWL or net count."""
    pads_u1 = [("1", 0.0, 0.0, 1, "SIG"), ("2", 1.0, 0.0, 2, "GND")]
    pads_u2 = [("1", 0.0, 0.0, 1, "SIG"), ("2", 1.0, 0.0, 2, "GND")]
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), pads_u1),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), pads_u2),
    ]
    p = _write(tmp_path, _board(fps, [(1, "SIG"), (2, "GND")]))

    bundle = placement_metric(p)
    assert bundle["total_hpwl_mm"] == 20.0  # GND span ignored
    assert bundle["signal_net_count"] == 1


def test_voltage_pattern_excluded_but_vin_is_signal(tmp_path: Path) -> None:
    """+3V3 is power (excluded); VIN is signal (Q2 default)."""
    assert classify_net("+3V3") == "power"
    assert classify_net("VIN") == "signal"
    assert classify_net("VOUT") == "signal"

    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0),
                   [("1", 0.0, 0.0, 1, "+3V3"), ("2", 1.0, 0.0, 2, "VIN")]),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0),
                   [("1", 0.0, 0.0, 1, "+3V3"), ("2", 1.0, 0.0, 2, "VIN")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "+3V3"), (2, "VIN")]))

    bundle = placement_metric(p)
    # Only VIN contributes: U1.2=(11,10), U2.2=(31,10) -> HPWL 20
    assert bundle["total_hpwl_mm"] == 20.0
    assert bundle["signal_net_count"] == 1


def test_high_fanout_cap(tmp_path: Path) -> None:
    """A 20-part signal net contributes 0 graph weight but still counts for HPWL."""
    fps = []
    nets = [(1, "BUS")]
    for k in range(20):
        ref = f"R{k}"
        fx = float(k * 2)  # spread out so HPWL is non-zero and large
        fps.append(_footprint("LibA:R", ref, (fx, 5.0, 0.0),
                              [("1", 0.0, 0.0, 1, "BUS")], courtyard=None))
    p = _write(tmp_path, _board(fps, nets))

    content = p.read_text(encoding="utf-8")
    pads = read_board_pads(content)
    graph = build_part_graph(build_net_pads(pads))
    # 20 > MAX_NET_FANOUT -> no proximity edges at all
    assert graph == {}

    bundle = placement_metric(p)
    # HPWL of BUS = (38-0) + (5-5) = 38
    assert bundle["total_hpwl_mm"] == 38.0
    assert bundle["signal_net_count"] == 1


def test_two_pin_and_five_part_graph_weights(tmp_path: Path) -> None:
    """Edge weight is 1/(m-1): 2-pin -> 1.0, a net on 3 parts -> 0.5 per pair."""
    fps = [
        _footprint("LibA:U", "U1", (0.0, 0.0, 0.0),
                   [("1", 0.0, 0.0, 1, "A"), ("2", 0.0, 1.0, 2, "T")]),
        _footprint("LibA:U", "U2", (5.0, 0.0, 0.0),
                   [("1", 0.0, 0.0, 1, "A"), ("2", 0.0, 1.0, 2, "T")]),
        _footprint("LibA:U", "U3", (10.0, 0.0, 0.0),
                   [("2", 0.0, 1.0, 2, "T")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "A"), (2, "T")]))
    graph = build_part_graph(build_net_pads(read_board_pads(p.read_text("utf-8"))))

    # Net A: 2 parts -> 1/(2-1)=1.0 on {U1,U2}
    assert graph[frozenset(("U1", "U2"))] == 1.5  # 1.0 from A + 0.5 from T
    # Net T: 3 parts -> 1/(3-1)=0.5 on each of {U1,U2},{U1,U3},{U2,U3}
    assert graph[frozenset(("U1", "U3"))] == 0.5
    assert graph[frozenset(("U2", "U3"))] == 0.5


def test_empty_board(tmp_path: Path) -> None:
    """A board whose pads are all net-0 -> HPWL 0, no scored parts, no crash."""
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 0, "")]),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), [("1", 0.0, 0.0, 0, "")]),
    ]
    p = _write(tmp_path, _board(fps, []))
    bundle = placement_metric(p)
    assert bundle["total_hpwl_mm"] == 0.0
    assert bundle["signal_net_count"] == 0
    assert bundle["scored_parts"] == 0


def test_determinism(tmp_path: Path) -> None:
    """Two calls on the same board return identical bundles (REQ-DET-001)."""
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "SIG")]))
    assert placement_metric(p) == placement_metric(p)


def test_pad_board_frame_rotation(tmp_path: Path) -> None:
    """A footprint at (20,20,90) with a pad at local (5,0) -> board (20,15).

    KiCad rotation is CCW on screen = CW in y-down file coordinates:
    (x,y) -> (x*c + y*s, -x*s + y*c) — verified against pcbnew-written pads
    (tests/test_rotation_convention.py).
    """
    fps = [
        _footprint("LibA:U", "U1", (20.0, 20.0, 90.0), [("1", 5.0, 0.0, 1, "SIG")]),
        _footprint("LibA:U", "U2", (40.0, 20.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "SIG")]))
    pads = read_board_pads(p.read_text("utf-8"))
    u1 = next(pd for pd in pads if pd["ref"] == "U1")
    assert math.isclose(u1["x_mm"], 20.0, abs_tol=1e-6)
    assert math.isclose(u1["y_mm"], 15.0, abs_tol=1e-6)


def test_no_outline_reports_null(tmp_path: Path) -> None:
    """No Edge.Cuts -> out_of_outline_count is null with a warning, no crash."""
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "SIG")], outline=None))
    bundle = placement_metric(p)
    assert bundle["out_of_outline_count"] is None
    assert any("no_board_outline" in w for w in bundle.get("warnings", []))


def test_pure_no_file_mutation(tmp_path: Path) -> None:
    """placement_metric writes nothing — board bytes are identical after."""
    fps = [
        _footprint("LibA:U", "U1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
        _footprint("LibA:U", "U2", (30.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    p = _write(tmp_path, _board(fps, [(1, "SIG")]))
    before = p.read_bytes()
    placement_metric(p)
    assert p.read_bytes() == before
