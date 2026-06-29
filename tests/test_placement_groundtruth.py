"""REQ-METRIC-006 ground-truth gate (Sprint P1, R5) — deterministic form.

This is the R5 gate: the placement metric MUST rank a worse placement worse than
a better placement **of the same netlist**. If it cannot, the metric is not
trustworthy and P2 (the net-aware placer that optimises against it) must not
start.

The spec's §7.2 V-M variant reconstructs the 2026-06-27 USB-C breakout live in
KiCad; that opt-in version lives in
``tests/integration/test_placement_metric_groundtruth.py``. This deterministic
version runs in the normal suite with no KiCad install, so the gate is enforced
on every CI run rather than only when an engineer opts in.

Construction: one fixed netlist (an IC between an input connector and an output
connector, plus two decoupling caps on the rails). Two placements of that exact
netlist:
  * **bad**  — IC, input and output connectors flung far apart, so the signal
               nets (IN0/IN1/OUT0/OUT1) span a large distance.
  * **good** — the same parts packed tightly, so the signal nets are short.
The decoupling caps sit on VCC/GND, which are power nets excluded from HPWL
(REQ-GRAPH-003); decap proximity is a P2 metric (``decap_*`` is null in P1), so
the P1 ground truth is a pure signal-net-HPWL comparison — exactly P1's headline.
"""

from __future__ import annotations

from pathlib import Path

from kicad_mcp.utils.placement_metrics import placement_metric

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

# The fixed netlist: (ref, lib_id, [(pad, net_name), ...]). Identical for both
# placements — only the per-ref coordinates below differ.
_NETLIST: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("U1", "Package_SO:SOIC-8", [
        ("1", "IN0"), ("2", "IN1"), ("4", "GND"),
        ("5", "OUT0"), ("6", "OUT1"), ("8", "VCC"),
    ]),
    ("J1", "Connector:Conn_01x02", [("1", "IN0"), ("2", "IN1")]),
    ("J2", "Connector:Conn_01x02", [("1", "OUT0"), ("2", "OUT1")]),
    ("C1", "Capacitor_SMD:C_0402", [("1", "VCC"), ("2", "GND")]),
    ("C2", "Capacitor_SMD:C_0402", [("1", "VCC"), ("2", "GND")]),
]

# Distinct net ids (0 is reserved for unconnected).
_NET_IDS = {"IN0": 1, "IN1": 2, "OUT0": 3, "OUT1": 4, "VCC": 5, "GND": 6}

# Two placements of the SAME netlist. Origins in mm.
_BAD_POS = {
    "U1": (60.0, 30.0), "J1": (0.0, 0.0), "J2": (120.0, 60.0),
    "C1": (10.0, 55.0), "C2": (110.0, 5.0),
}
_GOOD_POS = {
    "U1": (30.0, 30.0), "J1": (22.0, 30.0), "J2": (38.0, 30.0),
    "C1": (28.0, 27.0), "C2": (32.0, 27.0),
}


def _build(positions: dict[str, tuple[float, float]]) -> str:
    s = _HEADER
    s += '\t(net 0 "")\n'
    for name, nid in sorted(_NET_IDS.items(), key=lambda kv: kv[1]):
        s += f'\t(net {nid} "{name}")\n'
    # Generous outline so nothing is out-of-outline in either layout.
    s += (
        '\t(gr_rect (start -10 -10) (end 140 80) '
        '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
    )
    for ref, lib_id, pads in _NETLIST:
        fx, fy = positions[ref]
        s += f'\t(footprint "{lib_id}"\n'
        s += '\t\t(layer "F.Cu")\n'
        s += f"\t\t(at {fx} {fy} 0)\n"
        s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        s += f'\t\t(property "Value" "{ref}_v" (at 0 0 0) (layer "F.Fab"))\n'
        # Small courtyard so overlap/out-of-outline are computable (and 0 here).
        s += (
            "\t\t(fp_rect (start -1 -1) (end 1 1) "
            '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        )
        for i, (pad, net_name) in enumerate(pads):
            # Spread pads slightly so HPWL within a footprint is negligible but
            # nonzero; pad offsets are identical between layouts.
            px = float(i) * 0.5
            s += (
                f'\t\t(pad "{pad}" smd roundrect (at {px} 0) (size 0.3 0.3) '
                f'(layers "F.Cu" "F.Mask" "F.Paste") '
                f'(net {_NET_IDS[net_name]} "{net_name}"))\n'
            )
        s += "\t)\n"
    s += ")\n"
    return s


def test_metric_ranks_bad_worse_than_good(tmp_path: Path) -> None:
    """H_bad > H_good for the same netlist — the R5 trustworthiness gate."""
    bad = tmp_path / "bad.kicad_pcb"
    good = tmp_path / "good.kicad_pcb"
    bad.write_text(_build(_BAD_POS), encoding="utf-8")
    good.write_text(_build(_GOOD_POS), encoding="utf-8")

    m_bad = placement_metric(bad)
    m_good = placement_metric(good)

    h_bad = m_bad["total_hpwl_mm"]
    h_good = m_good["total_hpwl_mm"]

    # Same netlist => same signal-net count contributing to HPWL.
    assert m_bad["signal_net_count"] == m_good["signal_net_count"] == 4
    # Both layouts are legal (no overlaps, nothing outside the outline) so the
    # comparison is purely about wire length, not legality penalties.
    assert m_bad["overlap_count"] == m_good["overlap_count"] == 0
    assert m_bad["out_of_outline_count"] == m_good["out_of_outline_count"] == 0

    # The gate: the spread layout must score strictly worse, by a clear margin.
    margin = h_bad - h_good
    assert h_bad > h_good, f"metric did not rank bad worse: {h_bad} !> {h_good}"
    assert margin > 100.0, f"margin too small to trust the metric: {margin} mm"
