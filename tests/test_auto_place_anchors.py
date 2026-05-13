"""Tests for auto_place anchors parameter (Phase 6.2.2)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps


ASYMMETRIC_KICAD_MOD = textwrap.dedent("""\
    (footprint "Asymmetric_Test"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (fp_rect (start -2 -10) (end 8 2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
    )
""")


def _board_with(*entries) -> str:
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
    ]
    for e in entries:
        lines += [
            f'  (footprint "{e["footprint_lib_id"]}" (layer "F.Cu") (at {e["at_x"]} {e["at_y"]})',
            f'    (property "Reference" "{e["ref"]}" (at 0 0 0) (layer "F.Fab"))',
            f'    (property "Footprint" "{e["footprint_lib_id"]}" (at 0 0) (layer "F.Fab") (hide yes))',
            "  )",
        ]
    lines.append(")")
    return "\n".join(lines) + "\n"


def _extract_position(content: str, ref: str) -> tuple[float, float]:
    """Pull (at x y) out of the footprint block for *ref*."""
    import re
    m = re.search(
        rf'\(footprint\s+"[^"]+".*?\(property\s+"Reference"\s+"{re.escape(ref)}"',
        content, re.DOTALL,
    )
    assert m, f"footprint {ref} not found"
    snippet = content[m.start():m.end()]
    at_m = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)', snippet)
    assert at_m
    return (float(at_m.group(1)), float(at_m.group(2)))


def test_anchored_ref_position_unchanged(tmp_path: Path):
    """A ref listed in anchors must keep its original position after auto_place."""
    board = tmp_path / "anchored.kicad_pcb"
    # J1 starts at (40, 67) — typical "already anchored by place_at_edge" position
    board.write_text(_board_with(
        {"ref": "J1", "footprint_lib_id": "Test:Asymmetric", "at_x": 40.0, "at_y": 67.0},
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
        {"ref": "R1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=ASYMMETRIC_KICAD_MOD):
        result = ops.auto_place(
            board, board_x=10.0, board_y=10.0,
            board_width=60.0, board_height=50.0,
            clearance_mm=0.5,
            anchors=["J1"],
        )

    placed_refs = {p["reference"] for p in result["placements"]}
    assert "J1" not in placed_refs, "J1 was anchored — should not be in placements"
    assert "U1" in placed_refs
    assert "R1" in placed_refs

    content = board.read_text(encoding="utf-8")
    j1_x, j1_y = _extract_position(content, "J1")
    assert (j1_x, j1_y) == (40.0, 67.0), (
        f"Anchored J1 position changed from (40, 67) to ({j1_x}, {j1_y})"
    )


def test_no_anchors_is_back_compat(tmp_path: Path):
    """Calling auto_place without anchors must behave identically to before 6.2.2."""
    board = tmp_path / "back_compat.kicad_pcb"
    board.write_text(_board_with(
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
        {"ref": "R1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")
    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=ASYMMETRIC_KICAD_MOD):
        result = ops.auto_place(
            board, board_x=10.0, board_y=10.0,
            board_width=60.0, board_height=50.0,
            clearance_mm=0.5,
        )
    placed_refs = {p["reference"] for p in result["placements"]}
    assert placed_refs == {"U1", "R1"}


def test_empty_anchors_list_equivalent_to_none(tmp_path: Path):
    board = tmp_path / "empty_anchors.kicad_pcb"
    board.write_text(_board_with(
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")
    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=ASYMMETRIC_KICAD_MOD):
        result = ops.auto_place(
            board, board_x=10.0, board_y=10.0,
            board_width=60.0, board_height=50.0,
            clearance_mm=0.5,
            anchors=[],
        )
    assert result["components_placed"] == 1


def test_unknown_anchor_ref_does_not_raise(tmp_path: Path):
    """Listing a non-existent ref in anchors is a no-op, not an error."""
    board = tmp_path / "unknown_anchor.kicad_pcb"
    board.write_text(_board_with(
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")
    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=ASYMMETRIC_KICAD_MOD):
        result = ops.auto_place(
            board, board_x=10.0, board_y=10.0,
            board_width=60.0, board_height=50.0,
            clearance_mm=0.5,
            anchors=["does_not_exist"],
        )
    # U1 still placed; phantom anchor silently ignored
    assert result["components_placed"] == 1
