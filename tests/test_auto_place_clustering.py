"""Tests for sheet-hierarchy clustering in auto_place (Phase 6.3.3)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps


# A symmetric 5×5 mm footprint — placement geometry is uninteresting here,
# what we're testing is the order in which components get placed.
SQUARE_5MM_MOD = textwrap.dedent("""\
    (footprint "Square5"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (fp_rect (start -2.5 -2.5) (end 2.5 2.5) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
    )
""")


def _board(*entries) -> str:
    """Build a board with footprints that carry (path "/...") clauses."""
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
    ]
    for e in entries:
        path_clause = (
            f'    (path "{e["sheet_path"]}")\n'
            if e.get("sheet_path") else ""
        )
        lines += [
            f'  (footprint "Test:Square5" (layer "F.Cu") (at {e["at_x"]} {e["at_y"]})',
            f'    (uuid "test-{e["ref"].lower()}-uuid")',
            f'    (property "Reference" "{e["ref"]}" (at 0 0 0) (layer "F.Fab"))',
            f'    (property "Footprint" "Test:Square5" (at 0 0) (layer "F.Fab") (hide yes))',
            path_clause.rstrip("\n") if path_clause else "",
            "  )",
        ]
    lines.append(")")
    return "\n".join(l for l in lines if l) + "\n"


def test_components_from_same_sheet_are_placed_consecutively(tmp_path: Path):
    """Three components per sheet × two sheets — same-sheet refs land adjacent."""
    SHEET_A = "/aaaa-1111"
    SHEET_B = "/bbbb-2222"
    board = tmp_path / "two_sheets.kicad_pcb"
    board.write_text(_board(
        # Interleaved on disk to prove sort matters
        {"ref": "U1", "at_x": 0, "at_y": 0, "sheet_path": SHEET_A},
        {"ref": "U2", "at_x": 0, "at_y": 0, "sheet_path": SHEET_B},
        {"ref": "R1", "at_x": 0, "at_y": 0, "sheet_path": SHEET_A},
        {"ref": "R2", "at_x": 0, "at_y": 0, "sheet_path": SHEET_B},
        {"ref": "C1", "at_x": 0, "at_y": 0, "sheet_path": SHEET_A},
        {"ref": "C2", "at_x": 0, "at_y": 0, "sheet_path": SHEET_B},
    ), encoding="utf-8")

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=SQUARE_5MM_MOD):
        # REQ-BACK-002: same-sheet adjacency is a legacy row-packer property; the
        # net-aware default reorders by connectivity, so this test pins strategy.
        result = ops.auto_place(
            board, board_x=0, board_y=0,
            board_width=100, board_height=100,
            clearance_mm=0.5, strategy="row",
        )

    placed_order = [p["reference"] for p in result["placements"]]
    # Find the index of each sheet's components in the placement order
    sheet_a_indices = sorted(placed_order.index(r) for r in ("U1", "R1", "C1"))
    sheet_b_indices = sorted(placed_order.index(r) for r in ("U2", "R2", "C2"))

    # Each sheet's components must be contiguous in placement order
    assert sheet_a_indices == list(range(sheet_a_indices[0], sheet_a_indices[0] + 3)), (
        f"Sheet A refs not contiguous: {sheet_a_indices} in order {placed_order}"
    )
    assert sheet_b_indices == list(range(sheet_b_indices[0], sheet_b_indices[0] + 3)), (
        f"Sheet B refs not contiguous: {sheet_b_indices} in order {placed_order}"
    )


def test_flat_schematic_is_unchanged(tmp_path: Path):
    """A board with no (path ...) clauses falls into one group → same behavior as before."""
    board = tmp_path / "flat.kicad_pcb"
    board.write_text(_board(
        {"ref": "R1", "at_x": 0, "at_y": 0},
        {"ref": "R2", "at_x": 0, "at_y": 0},
        {"ref": "U1", "at_x": 0, "at_y": 0},
    ), encoding="utf-8")

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=SQUARE_5MM_MOD):
        result = ops.auto_place(
            board, board_x=0, board_y=0,
            board_width=100, board_height=100,
            clearance_mm=0.5,
        )
    # All three should be placed (single group, no clustering surprises)
    assert result["components_placed"] == 3


def test_parser_extracts_sheet_path_from_instances():
    """_parse_sch_symbol pulls (path "/UUID") out of the instances block."""
    from kicad_mcp.backends.file_backend import _parse_sch_symbol

    node = [
        "symbol",
        ["lib_id", "Device:R"],
        ["property", "Reference", "R1"],
        ["instances",
            ["project", "",
                ["path", "/aaaa-1111/bbbb-2222",
                    ["reference", "R1"], ["unit", 1],
                ],
            ],
        ],
    ]
    sym = _parse_sch_symbol(node)
    assert sym is not None
    assert sym["sheet_path"] == "/aaaa-1111/bbbb-2222"


def test_parser_no_instances_block_no_sheet_path():
    from kicad_mcp.backends.file_backend import _parse_sch_symbol

    node = [
        "symbol",
        ["lib_id", "Device:R"],
        ["property", "Reference", "R1"],
    ]
    sym = _parse_sch_symbol(node)
    assert sym is not None
    assert "sheet_path" not in sym


# ---------------------------------------------------------------------------
# Sync → injection roundtrip
# ---------------------------------------------------------------------------

def test_inject_footprint_sheet_path_adds_clause(tmp_path: Path):
    """_inject_footprint_sheet_path adds (path "...") to an existing footprint."""
    from kicad_mcp.tools.schematic import _inject_footprint_sheet_path

    board = tmp_path / "no_path.kicad_pcb"
    board.write_text(_board(
        {"ref": "R1", "at_x": 10, "at_y": 10},
    ), encoding="utf-8")

    _inject_footprint_sheet_path(board, "R1", "/aaaa-1111")
    content = board.read_text(encoding="utf-8")
    assert '(path "/aaaa-1111")' in content


def test_inject_footprint_sheet_path_overwrites_existing(tmp_path: Path):
    from kicad_mcp.tools.schematic import _inject_footprint_sheet_path

    board = tmp_path / "with_path.kicad_pcb"
    board.write_text(_board(
        {"ref": "R1", "at_x": 10, "at_y": 10, "sheet_path": "/old"},
    ), encoding="utf-8")

    _inject_footprint_sheet_path(board, "R1", "/new")
    content = board.read_text(encoding="utf-8")
    assert '(path "/new")' in content
    assert '(path "/old")' not in content


# ---------------------------------------------------------------------------
# ClusterId override (PlacementIntent: cluster:NAME)
# ---------------------------------------------------------------------------

def _board_with_cluster_ids(*entries) -> str:
    """Board fixture where each entry can carry a ClusterId property."""
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
    ]
    for e in entries:
        cluster_prop = ""
        if e.get("cluster_id"):
            cluster_prop = f'    (property "ClusterId" "{e["cluster_id"]}")\n'
        lines += [
            f'  (footprint "Test:Square5" (layer "F.Cu") (at {e["at_x"]} {e["at_y"]})',
            f'    (uuid "test-{e["ref"].lower()}-uuid")',
            f'    (property "Reference" "{e["ref"]}" (at 0 0 0) (layer "F.Fab"))',
            f'    (property "Footprint" "Test:Square5" (at 0 0) (layer "F.Fab") (hide yes))',
            cluster_prop.rstrip("\n") if cluster_prop else "",
            "  )",
        ]
    lines.append(")")
    return "\n".join(l for l in lines if l) + "\n"


def test_cluster_id_groups_independent_of_sheet(tmp_path: Path):
    """ClusterId is an override — refs with the same ClusterId cluster together
    even if their sheet_path differs (or is absent)."""
    board = tmp_path / "cluster_id.kicad_pcb"
    board.write_text(_board_with_cluster_ids(
        {"ref": "U1", "at_x": 0, "at_y": 0, "cluster_id": "rf_block"},
        {"ref": "R2", "at_x": 0, "at_y": 0, "cluster_id": "audio_block"},
        {"ref": "L1", "at_x": 0, "at_y": 0, "cluster_id": "rf_block"},
        {"ref": "C2", "at_x": 0, "at_y": 0, "cluster_id": "audio_block"},
        {"ref": "C1", "at_x": 0, "at_y": 0, "cluster_id": "rf_block"},
        {"ref": "R3", "at_x": 0, "at_y": 0, "cluster_id": "audio_block"},
    ), encoding="utf-8")

    ops = FileBoardOps()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=SQUARE_5MM_MOD):
        # REQ-BACK-002: ClusterId adjacency is a legacy row-packer property.
        result = ops.auto_place(
            board, board_x=0, board_y=0,
            board_width=100, board_height=100,
            clearance_mm=0.5, strategy="row",
        )

    placed_order = [p["reference"] for p in result["placements"]]
    rf = sorted(placed_order.index(r) for r in ("U1", "L1", "C1"))
    audio = sorted(placed_order.index(r) for r in ("R2", "C2", "R3"))
    assert rf == list(range(rf[0], rf[0] + 3)), f"rf_block not contiguous: {placed_order}"
    assert audio == list(range(audio[0], audio[0] + 3)), f"audio_block not contiguous: {placed_order}"


def test_inject_footprint_property_adds_cluster_id(tmp_path: Path):
    """_inject_footprint_property adds ClusterId to an existing footprint."""
    from kicad_mcp.tools.schematic import _inject_footprint_property

    board = tmp_path / "no_cluster.kicad_pcb"
    board.write_text(_board(
        {"ref": "R1", "at_x": 10, "at_y": 10},
    ), encoding="utf-8")

    _inject_footprint_property(board, "R1", "ClusterId", "rf_block")
    content = board.read_text(encoding="utf-8")
    assert '(property "ClusterId" "rf_block")' in content
