"""Tests for validate_connector_orientations (drc.py, Phase 6.1.2)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _board(
    bbox: tuple[float, float, float, float] | None,
    *footprints: dict,
) -> str:
    """Build a minimal .kicad_pcb with an Edge.Cuts rect and footprints.

    Each footprint dict: {ref, at_x, at_y, rotation, pcb_edge=(lx, ly), pads=[(px,py)...]}
    """
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
    ]
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        lines += [
            f'  (gr_rect (start {x0} {y0}) (end {x1} {y1}) '
            f'(stroke (width 0.05)) (layer "Edge.Cuts"))',
        ]
    for fp in footprints:
        ref = fp["ref"]
        ax = fp["at_x"]
        ay = fp["at_y"]
        rot = fp.get("rotation", 0)
        lib_id = fp.get("lib_id", "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal")
        at_clause = f"{ax} {ay} {rot}" if rot else f"{ax} {ay}"
        lines += [
            f'  (footprint "{lib_id}" (layer "F.Cu") (at {at_clause})',
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab"))',
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


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _call_tool(board_path: Path) -> dict:
    import fastmcp
    from kicad_mcp.tools import drc
    from kicad_mcp.utils.change_log import ChangeLog

    backend_stub = MagicMock()
    change_log = ChangeLog(board_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    drc.register_tools(mcp, backend_stub, change_log)
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "validate_connector_orientations"
    )
    return json.loads(tool_fn(str(board_path)))


# ---------------------------------------------------------------------------
# Pass cases
# ---------------------------------------------------------------------------

def test_connector_at_south_edge_facing_south_passes(tmp_path: Path):
    """Connector with local '+y' face at south edge, rotation 0 → board face +y, passes."""
    board = _write(tmp_path, "south_ok.kicad_pcb", _board(
        (0, 0, 80, 70),  # board bbox
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),  # local +y mating face
        },
    ))
    result = _call_tool(board)
    assert result["status"] == "success"
    assert result["passed"] is True
    assert result["checked"] == 1
    assert result["violations"] == []


def test_connector_facing_inward_fails(tmp_path: Path):
    """Connector with local '+y' face at south edge, rotation 180 → board face -y, FAILS."""
    board = _write(tmp_path, "inward.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 180,
            "pcb_edge": (0.0, 3.65),  # local +y, rotated 180 → board -y (inward)
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is False
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["ref"] == "J1"
    assert v["closest_edge"] == "south"
    assert v["current_face_in_board_frame"] == "-y"
    # Suggested rotation to bring local +y → board +y (south outward) is 0°
    assert v["suggested_rotation"] in (0.0, 360.0)


@pytest.mark.parametrize(
    "edge_label,at_x,at_y,rotation,local_face_xy,should_pass",
    [
        # All four edges, connector facing outward → all should pass
        ("south", 40.0, 67.0, 0,   (0.0, 3.65),  True),
        ("north", 40.0, 3.0,  180, (0.0, 3.65),  True),   # 180° → board -y, north outward = -y
        ("east",  77.0, 35.0, 270, (0.0, 3.65),  True),   # 270° rotates +y→+x, east outward = +x
        ("west",  3.0,  35.0, 90,  (0.0, 3.65),  True),   # 90° rotates +y→-x, west outward = -x
    ],
)
def test_all_four_edges_outward(tmp_path: Path, edge_label, at_x, at_y, rotation, local_face_xy, should_pass):
    board = _write(tmp_path, f"{edge_label}.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": at_x, "at_y": at_y, "rotation": rotation,
            "pcb_edge": local_face_xy,
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is should_pass, (
        f"Edge {edge_label} rot {rotation}: expected passed={should_pass}, "
        f"got {result['passed']} with violations={result['violations']}"
    )


# ---------------------------------------------------------------------------
# Empty / no-edge-facing cases
# ---------------------------------------------------------------------------

def test_empty_board_passes(tmp_path: Path):
    """A board with no edge-facing connectors must not be blocked."""
    board = _write(tmp_path, "empty.kicad_pcb", _board((0, 0, 80, 70)))
    result = _call_tool(board)
    assert result["passed"] is True
    assert result["checked"] == 0
    assert result["violations"] == []


def test_no_connectors_just_resistors(tmp_path: Path):
    board = _write(tmp_path, "smd_only.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "R1", "at_x": 30.0, "at_y": 30.0,
            "lib_id": "Resistor_SMD:R_0603_1608Metric",
            "pads": [(-0.8, 0), (0.8, 0)],
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is True
    assert result["checked"] == 0


# ---------------------------------------------------------------------------
# Missing board outline → fails with specific violation
# ---------------------------------------------------------------------------

def test_missing_edge_cuts_outline_fails(tmp_path: Path):
    board = _write(tmp_path, "no_outline.kicad_pcb", _board(
        None,  # no Edge.Cuts geometry
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is False
    assert any(v["type"] == "no_board_outline" for v in result["violations"])


# ---------------------------------------------------------------------------
# Indeterminate case — name-match without resolvable mating face
# ---------------------------------------------------------------------------

def test_name_match_no_pads_is_indeterminate(tmp_path: Path):
    """A connector that matches by name but has no pads → indeterminate, not violation."""
    board = _write(tmp_path, "indet.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 0,
            "lib_id": "Connector_Audio:Custom_Horizontal_Jack",
            # no pcb_edge marker, no pads
        },
    ))
    result = _call_tool(board)
    # No violation but reported in indeterminate
    assert result["passed"] is True
    assert len(result["indeterminate"]) == 1
    assert result["indeterminate"][0]["ref"] == "J1"


# ---------------------------------------------------------------------------
# Sidecar cache: result is written and survives subsequent reads
# ---------------------------------------------------------------------------

def test_cache_written_on_pass(tmp_path: Path):
    from kicad_mcp.utils.validation_cache import get_validation

    board = _write(tmp_path, "cache_pass.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is True

    cached = get_validation(board, "validate_connector_orientations")
    assert cached is not None
    assert cached["passed"] is True


def test_cache_invalidated_when_board_changes(tmp_path: Path):
    """Editing the board byte content must invalidate the cached pass."""
    from kicad_mcp.utils.validation_cache import get_validation

    board = _write(tmp_path, "cache_inval.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 40.0, "at_y": 67.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),
        },
    ))
    result = _call_tool(board)
    assert result["passed"] is True

    # Tamper with the file
    text = board.read_text(encoding="utf-8")
    board.write_text(text + "\n", encoding="utf-8")

    cached = get_validation(board, "validate_connector_orientations")
    assert cached is None, "Cache must invalidate when board hash changes"


# ---------------------------------------------------------------------------
# Real-world regression: bt_audio_v1 BEFORE the fix → must fail
# ---------------------------------------------------------------------------

REAL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "boards"
    / "bt_audio_v1_before_connector_fix.kicad_pcb"
)


@pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="real-world fixture not present")
def test_bt_audio_v1_before_fix_fails(tmp_path: Path):
    """The pre-fix bt_audio_v1 board had J2/J3 facing inward — must fail validation.

    Copy the read-only fixture into tmp so the sidecar cache write doesn't
    pollute tests/fixtures/.
    """
    import shutil
    scratch = tmp_path / "bt_audio_v1.kicad_pcb"
    shutil.copyfile(REAL_FIXTURE, scratch)
    result = _call_tool(scratch)

    assert result["status"] == "success"
    assert result["passed"] is False, (
        f"Expected pre-fix bt_audio_v1 to fail validation, got passed=True. "
        f"checked={result['checked']}, violations={result['violations']}"
    )
    # At least J2 or J3 should violate (J1 USB-C may or may not depending on its placement)
    violation_refs = {v["ref"] for v in result["violations"] if "ref" in v}
    assert violation_refs & {"J2", "J3"}, (
        f"Expected J2 or J3 in violations, got {violation_refs}"
    )
