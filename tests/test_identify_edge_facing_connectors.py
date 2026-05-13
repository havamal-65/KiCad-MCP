"""Tests for identify_edge_facing_connectors (drc.py MCP tool, Phase 6.1.1)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Synthetic .kicad_pcb fixture builders
# ---------------------------------------------------------------------------

_HEADER = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
""")
_FOOTER = ")\n"


def _fp_block(
    lib_id: str,
    ref: str,
    at_x: float,
    at_y: float,
    rotation: float = 0.0,
    *,
    pcb_edge: tuple[float, float] | None = None,
    pads: tuple[tuple[float, float], ...] = (),
    attr: str | None = None,
) -> str:
    """Build a single (footprint ...) block. Inner geometry is in local frame."""
    at_clause = f"{at_x} {at_y} {rotation}" if rotation else f"{at_x} {at_y}"
    lines = [
        f'  (footprint "{lib_id}" (layer "F.Cu") (at {at_clause})',
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab"))',
    ]
    if attr:
        lines.append(f"    (attr {attr})")
    for px, py in pads:
        lines.append(
            f'    (pad "1" smd rect (at {px} {py}) (size 1.0 1.0) '
            f'(layers "F.Cu" "F.Paste" "F.Mask"))'
        )
    if pcb_edge is not None:
        ex, ey = pcb_edge
        lines += [
            f'    (fp_text user "PCB edge"',
            f'      (at {ex} {ey} 0)',
            f'      (layer "Dwgs.User")',
            f'    )',
        ]
    lines.append("  )")
    return "\n".join(lines) + "\n"


def _board(*blocks: str) -> str:
    return _HEADER + "".join(blocks) + _FOOTER


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _call_tool(board_path: Path) -> dict:
    """Drive identify_edge_facing_connectors via the registered MCP tool."""
    import fastmcp
    from kicad_mcp.tools import drc
    from kicad_mcp.utils.change_log import ChangeLog

    backend_stub = MagicMock()
    change_log = ChangeLog(board_path.parent / "changes.json")

    mcp = fastmcp.FastMCP("test")
    drc.register_tools(mcp, backend_stub, change_log)

    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "identify_edge_facing_connectors"
    )
    return json.loads(tool_fn(str(board_path)))


# ---------------------------------------------------------------------------
# Signal 1: "PCB edge" marker → high confidence + face derivation
# ---------------------------------------------------------------------------

def test_pcb_edge_marker_high_confidence(tmp_path: Path):
    """Marker on +y side of footprint origin → mating_face '+y', confidence 'high'."""
    board = _write(tmp_path, "marker.kicad_pcb", _board(
        _fp_block(
            "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal",
            "J1", at_x=20.0, at_y=10.0, rotation=0,
            pcb_edge=(0.0, 3.65),  # +y side of footprint origin
            pads=((-1.0, -3.0), (1.0, -3.0)),
        ),
    ))
    result = _call_tool(board)
    assert result["status"] == "success"
    assert result["checked_count"] == 1
    assert len(result["connectors"]) == 1
    c = result["connectors"][0]
    assert c["ref"] == "J1"
    assert c["mating_face"] == "+y"
    assert c["confidence"] == "high"
    assert "PCB edge" in c["evidence"]


@pytest.mark.parametrize(
    "marker_xy,expected_face",
    [
        ((5.0, 0.0), "+x"),
        ((-5.0, 0.0), "-x"),
        ((0.0, 5.0), "+y"),
        ((0.0, -5.0), "-y"),
        ((3.0, 1.0), "+x"),    # dominant axis wins
        ((1.0, 3.0), "+y"),
        ((-2.0, -1.0), "-x"),  # tie goes to x (>= rule)
    ],
)
def test_face_derivation_from_marker_position(tmp_path: Path, marker_xy, expected_face):
    board = _write(tmp_path, f"face_{expected_face}.kicad_pcb", _board(
        _fp_block(
            "Connector_USB:USB_C_Receptacle_test",
            "J1", at_x=0.0, at_y=0.0,
            pcb_edge=marker_xy,
        ),
    ))
    result = _call_tool(board)
    assert result["connectors"][0]["mating_face"] == expected_face


def test_marker_face_is_local_frame_not_board_frame(tmp_path: Path):
    """A rotated footprint still reports the marker in LOCAL frame.

    The outer (at x y rotation) is the footprint's placement; the (fp_text)'s
    inner (at lx ly) is the marker in the footprint's own coordinate system.
    Conversion to board frame is 6.1.2's job, not 6.1.1's.
    """
    board = _write(tmp_path, "rotated.kicad_pcb", _board(
        _fp_block(
            "Connector_JST:JST_PH_Horizontal",
            "J1", at_x=20.0, at_y=10.0, rotation=180,  # placed rotated
            pcb_edge=(0.0, 3.65),  # marker still in local +y
        ),
    ))
    result = _call_tool(board)
    # Marker says local +y regardless of outer rotation
    assert result["connectors"][0]["mating_face"] == "+y"


# ---------------------------------------------------------------------------
# Signal 2: Footprint name heuristic → medium confidence
# ---------------------------------------------------------------------------

def test_horizontal_name_match_no_marker_medium(tmp_path: Path):
    """Name contains 'Horizontal' but no marker → medium confidence."""
    board = _write(tmp_path, "name.kicad_pcb", _board(
        _fp_block(
            "Connector_Generic:Custom_Horizontal_Connector",
            "J1", at_x=10.0, at_y=10.0,
            pads=((0.0, -3.0), (2.0, -3.0)),  # pads on -y side
        ),
    ))
    result = _call_tool(board)
    assert result["checked_count"] == 1
    c = result["connectors"][0]
    assert c["confidence"] == "medium"
    # Pads on -y → mating face on +y (opposite the cluster)
    assert c["mating_face"] == "+y"
    assert "name match" in c["evidence"]


def test_name_match_no_pads_returns_none_face(tmp_path: Path):
    """Name matches but no pads to compute centroid from → mating_face=None."""
    board = _write(tmp_path, "noisy.kicad_pcb", _board(
        _fp_block(
            "Connector_Audio:Custom_Horizontal_Jack",
            "J1", at_x=10.0, at_y=10.0,
            pads=(),  # no pads
        ),
    ))
    result = _call_tool(board)
    c = result["connectors"][0]
    assert c["confidence"] == "medium"
    assert c["mating_face"] is None


# ---------------------------------------------------------------------------
# Negative case: SMD chip should NOT be flagged
# ---------------------------------------------------------------------------

def test_smd_resistor_not_flagged(tmp_path: Path):
    board = _write(tmp_path, "smd.kicad_pcb", _board(
        _fp_block(
            "Resistor_SMD:R_0603_1608Metric",
            "R1", at_x=10.0, at_y=10.0,
            pads=((-0.8, 0.0), (0.8, 0.0)),
        ),
    ))
    result = _call_tool(board)
    assert result["checked_count"] == 1
    assert result["connectors"] == []


def test_vertical_pin_header_not_flagged(tmp_path: Path):
    """A 'Vertical' through-hole pin header is not edge-facing."""
    board = _write(tmp_path, "vertical.kicad_pcb", _board(
        _fp_block(
            "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
            "J1", at_x=10.0, at_y=10.0,
            pads=((0.0, 0.0), (2.54, 0.0), (5.08, 0.0), (7.62, 0.0)),
        ),
    ))
    result = _call_tool(board)
    # No "PCB edge" marker, no _EDGE_NAME_TOKENS match → not flagged
    assert result["connectors"] == []


# ---------------------------------------------------------------------------
# Reference designators starting with # (power flags etc.) are skipped
# ---------------------------------------------------------------------------

def test_hash_prefix_reference_skipped(tmp_path: Path):
    board = _write(tmp_path, "pwr.kicad_pcb", _board(
        _fp_block(
            "Connector_USB:USB_C_Receptacle_internal",
            "#PWR01", at_x=10.0, at_y=10.0,
            pcb_edge=(0.0, 5.0),
        ),
    ))
    result = _call_tool(board)
    # Skipped — not counted, not returned
    assert result["checked_count"] == 0
    assert result["connectors"] == []


# ---------------------------------------------------------------------------
# Empty board passes (no footprints, no connectors)
# ---------------------------------------------------------------------------

def test_empty_board(tmp_path: Path):
    board = _write(tmp_path, "empty.kicad_pcb", _board())
    result = _call_tool(board)
    assert result["status"] == "success"
    assert result["checked_count"] == 0
    assert result["connectors"] == []


# ---------------------------------------------------------------------------
# Real-world regression: bt_audio_v1 before connector orientation fix
# ---------------------------------------------------------------------------

REAL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "boards"
    / "bt_audio_v1_before_connector_fix.kicad_pcb"
)


@pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="real-world fixture not present")
def test_bt_audio_v1_detects_three_connectors():
    """Real-world regression: bt_audio_v1 has J1 (USB-C), J2 (JST), J3 (audio jack).

    Of the three, only J3 (audio jack) has a 'PCB edge' marker in its KiCad
    library source — the other two are detected by the name heuristic on
    'Horizontal' / 'Connector_USB'. All three must be flagged as edge-facing
    and all three must resolve a non-None mating_face direction.
    """
    result = _call_tool(REAL_FIXTURE)
    assert result["status"] == "success"
    flagged = {c["ref"]: c for c in result["connectors"]}
    for ref in ("J1", "J2", "J3"):
        assert ref in flagged, f"Expected {ref} flagged as edge-facing"
        assert flagged[ref]["mating_face"] is not None, (
            f"{ref} mating_face is None — pad centroid extraction failed"
        )
    # J3 should be high-confidence (has the marker)
    assert flagged["J3"]["confidence"] == "high", (
        f"J3 expected high (marker present), got {flagged['J3']['confidence']}"
    )
