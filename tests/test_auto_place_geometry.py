"""Tests for auto_place anchor offset — guards Bug 4a (asymmetric courtyards)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps


# Asymmetric fixture: anchor at (0, 0), courtyard from (-2, -10) to (+8, +2).
# A symmetric fixture would have courtyard at (-w/2, -h/2) to (+w/2, +h/2),
# so any test passing under symmetric fixtures gives no signal about Bug 4a.
ASYMMETRIC_KICAD_MOD = textwrap.dedent("""\
    (footprint "Asymmetric_Test"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (property "Reference" "REF**" (at 0 -11 0) (layer "F.SilkS"))
      (property "Value" "Asymmetric_Test" (at 0 3 0) (layer "F.Fab"))
      (fp_rect (start -2 -10) (end 8 2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
    )
""")


SYMMETRIC_0402_MOD = textwrap.dedent("""\
    (footprint "R_0402"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (fp_rect (start -0.5 -0.5) (end 0.5 0.5) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
      (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    )
""")


def _board_with(*entries) -> str:
    """Build a minimal .kicad_pcb with one footprint per entry.

    Each entry is a dict: {ref, footprint_lib_id, at_x, at_y}.
    """
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


@pytest.fixture
def asymmetric_board(tmp_path: Path) -> Path:
    f = tmp_path / "asym.kicad_pcb"
    f.write_text(_board_with(
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")
    return f


def test_anchor_offset_aligns_courtyard_top_left_to_cursor(asymmetric_board: Path):
    """The bug: anchor was placed at cursor + w/2; courtyard escaped its row.

    Fix: anchor placed so courtyard's top-left lands at (cursor_x, cursor_y).
    With cursor at (10 + clearance, 10 + clearance) and clearance=0.5, board origin at (10, 10):
      cursor = (10.5, 10.5)
      courtyard xmin=-2, ymin=-10
      → expected anchor = (cursor.x - xmin, cursor.y - ymin) = (12.5, 20.5)
    """
    def fake_load(lib_id: str, project_dir=None):
        return ASYMMETRIC_KICAD_MOD

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load):
        result = ops.auto_place(
            asymmetric_board,
            board_x=10.0, board_y=10.0,
            board_width=100.0, board_height=80.0,
            clearance_mm=0.5,
        )

    placements = {p["reference"]: p for p in result["placements"]}
    assert "U1" in placements, f"U1 not placed; result={result}"
    u1 = placements["U1"]
    # cursor = board_origin + clearance = (10.5, 10.5)
    # anchor = cursor - (xmin, ymin) = (10.5 - (-2), 10.5 - (-10)) = (12.5, 20.5)
    assert u1["x"] == pytest.approx(12.5), \
        f"Expected anchor.x=12.5 (cursor.x - xmin), got {u1['x']}"
    assert u1["y"] == pytest.approx(20.5), \
        f"Expected anchor.y=20.5 (cursor.y - ymin), got {u1['y']}"


def test_no_overlap_after_placement_with_asymmetric_neighbour(tmp_path: Path):
    """Place the asymmetric module + a 0402 passive; verify no courtyard overlap.

    Under the old code, the asymmetric module's anchor was placed at (cursor + w/2);
    its courtyard extended above the cursor row, overlapping anything placed there.
    Under the fix, the courtyard's top-left aligns with the cursor → no overlap.
    """
    board_file = tmp_path / "mixed.kicad_pcb"
    board_file.write_text(_board_with(
        {"ref": "U1", "footprint_lib_id": "Test:Asymmetric", "at_x": 0.0, "at_y": 0.0},
        {"ref": "R1", "footprint_lib_id": "Test:R0402", "at_x": 0.0, "at_y": 0.0},
    ), encoding="utf-8")

    def fake_load(lib_id: str, project_dir=None):
        if "Asymmetric" in lib_id:
            return ASYMMETRIC_KICAD_MOD
        return SYMMETRIC_0402_MOD

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load):
        ops.auto_place(
            board_file,
            board_x=10.0, board_y=10.0,
            board_width=50.0, board_height=50.0,
            clearance_mm=0.5,
        )

    # Now run check_courtyard_overlaps and verify passed
    import json
    import fastmcp
    from unittest.mock import MagicMock
    from kicad_mcp.utils.change_log import ChangeLog
    from kicad_mcp.tools import drc
    backend_stub = MagicMock()
    change_log = ChangeLog(tmp_path / "changes.json")
    mcp = fastmcp.FastMCP("test")
    drc.register_tools(mcp, backend_stub, change_log)
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values()
                   if t.name == "check_courtyard_overlaps")
    overlap_result = json.loads(tool_fn(str(board_file)))
    assert overlap_result["passed"] is True, \
        f"Expected no overlaps after auto_place, got {overlap_result['overlap_count']}: {overlap_result.get('overlaps')}"
