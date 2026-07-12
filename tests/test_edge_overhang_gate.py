"""#18 (F2/S3) — the placement gate's edge-connector overhang exemption.

REQ-EDGE-1: an edge-anchored connector whose mating face points off-board may
legally overhang Edge.Cuts (USB4085-class datasheet overhang) — its courtyard
past the edge no longer counts toward blocking ``out_of_outline``.
REQ-EDGE-2 (R2, no masking): the exemption is narrow — non-connectors,
wrong-facing connectors, and corner crossings all still fail the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.drc import run_validate_placement_quality
from kicad_mcp.utils.placement_metrics import placement_metric


def _board(*footprints: dict) -> str:
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
        '  (gr_rect (start 0 0) (end 80 70) (stroke (width 0.05)) '
        '(layer "Edge.Cuts"))',
    ]
    for fp in footprints:
        ref = fp["ref"]
        rot = fp.get("rotation", 0)
        at_clause = f"{fp['at_x']} {fp['at_y']} {rot}" if rot else f"{fp['at_x']} {fp['at_y']}"
        cy = fp.get("courtyard", (-2.0, -1.5, 4.0, 6.5))
        lib_id = fp.get("lib_id", "Connector_JST:JST_PH_Horizontal")
        lines += [
            f'  (footprint "{lib_id}" (layer "F.Cu") (at {at_clause})',
            f'    (uuid "test-{ref.lower()}-uuid")',
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab"))',
            f'    (fp_rect (start {cy[0]} {cy[1]}) (end {cy[2]} {cy[3]}) '
            f'(layer "F.CrtYd") (stroke (width 0.05)))',
        ]
        if "pcb_edge" in fp:
            ex, ey = fp["pcb_edge"]
            lines += [
                f'    (fp_text user "PCB edge" (at {ex} {ey} 0) (layer "Dwgs.User"))',
            ]
        for px, py in fp.get("pads", []):
            lines += [
                f'    (pad "1" smd rect (at {px} {py}) (size 1 1) (layers "F.Cu"))',
            ]
        lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "gate.kicad_pcb"
    p.write_text(content, encoding="utf-8")
    return p


# Board outline is (0, 0)-(80, 70). Courtyard (-2, -1.5, 4, 6.5) at rot 0
# placed at y=66 spans board-y 64.5..72.5 → crosses ONLY the south edge (70).
_OVERHANG_SOUTH = {
    "ref": "J1", "at_x": 40.0, "at_y": 66.0, "rotation": 0,
    "pcb_edge": (0.0, 3.65),  # local mating face +y → points at south at rot 0
    "courtyard": (-2.0, -1.5, 4.0, 6.5),
}


def test_edge_connector_overhang_exempt(tmp_path: Path):
    """REQ-EDGE-1: outward-facing edge connector overhang doesn't count."""
    board = _write(tmp_path, _board(_OVERHANG_SOUTH))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 0, metric
    exemptions = metric.get("edge_overhang_exemptions", [])
    assert [e["reference"] for e in exemptions] == ["J1"]
    assert "south" in exemptions[0]["evidence"]

    gate = run_validate_placement_quality(board)
    assert not [v for v in gate["violations"] if v["type"] == "out_of_outline"]
    assert gate["passed"] is True, gate


def test_non_connector_overhang_still_fails(tmp_path: Path):
    """REQ-EDGE-2 / R2: a resistor past the outline is a genuine violation."""
    board = _write(tmp_path, _board({
        "ref": "R1", "at_x": 40.0, "at_y": 70.5,
        "lib_id": "Resistor_SMD:R_0805_2012Metric",
        "courtyard": (-1.7, -1.2, 1.7, 1.2),
    }))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 1, metric
    assert "edge_overhang_exemptions" not in metric

    gate = run_validate_placement_quality(board)
    assert [v for v in gate["violations"] if v["type"] == "out_of_outline"]
    assert gate["passed"] is False


def test_inward_facing_connector_overhang_still_fails(tmp_path: Path):
    """A connector overhanging an edge its mating face does NOT point at is a
    real placement error (mis-rotation), not a legal mating overhang."""
    fp = dict(_OVERHANG_SOUTH)
    fp["rotation"] = 180  # mating face now points -y (north), overhang at south
    fp["at_y"] = 72.0     # rotated courtyard y: 65.5..73.5 → crosses south only
    board = _write(tmp_path, _board(fp))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 1, metric

    gate = run_validate_placement_quality(board)
    assert gate["passed"] is False


def test_corner_crossing_not_exempt(tmp_path: Path):
    """Crossing two edges is not a mating overhang — still fails."""
    fp = dict(_OVERHANG_SOUTH)
    fp["at_x"] = 78.0  # courtyard x: 76..82 also crosses east (80)
    board = _write(tmp_path, _board(fp))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 1, metric


def test_indeterminate_face_falls_back_to_crossed_edge(tmp_path: Path):
    """F2-Q2 fallback: a name-matched connector whose mating face is
    indeterminate (symmetric body) is exempt on a SINGLE crossed edge."""
    board = _write(tmp_path, _board({
        "ref": "J2", "at_x": 40.0, "at_y": 66.0,
        "lib_id": "Connector_JST:JST_PH_Horizontal",
        "courtyard": (-3.0, -3.0, 3.0, 6.0),   # center (0, 1.5)
        "pads": [(-1.0, 1.5), (1.0, 1.5)],     # centroid (0, 1.5) — coincident
    }))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 0, metric
    exemptions = metric.get("edge_overhang_exemptions", [])
    assert [e["reference"] for e in exemptions] == ["J2"]
    assert "fallback" in exemptions[0]["evidence"]


def test_mixed_board_counts_only_genuine_offenders(tmp_path: Path):
    """Exempt connector + genuine offender on one board: count reflects only
    the real violation and the exemption is reported alongside it."""
    board = _write(tmp_path, _board(
        _OVERHANG_SOUTH,
        {
            "ref": "R1", "at_x": 10.0, "at_y": 70.5,
            "lib_id": "Resistor_SMD:R_0805_2012Metric",
            "courtyard": (-1.7, -1.2, 1.7, 1.2),
        },
    ))
    metric = placement_metric(board)
    assert metric["out_of_outline_count"] == 1, metric
    exemptions = metric.get("edge_overhang_exemptions", [])
    assert [e["reference"] for e in exemptions] == ["J1"]

    gate = run_validate_placement_quality(board)
    assert gate["passed"] is False  # R1 still blocks
