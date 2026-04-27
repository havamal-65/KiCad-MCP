"""Tests for FileBoardOps — pure file parsing/writing, no KiCad required."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps


# ---------------------------------------------------------------------------
# get_board_info
# ---------------------------------------------------------------------------

def test_get_board_info_returns_component_count(tmp_board: Path):
    ops = FileBoardOps()
    info = ops.get_board_info(tmp_board)
    assert info["num_components"] == 2
    assert info["num_nets"] == 3  # net 0, 1, 2


def test_get_board_info_empty_board(tmp_empty_board: Path):
    ops = FileBoardOps()
    info = ops.get_board_info(tmp_empty_board)
    assert info["num_components"] == 0
    assert info["num_nets"] == 1  # net 0 only


# ---------------------------------------------------------------------------
# get_components
# ---------------------------------------------------------------------------

def test_get_components_finds_both_refs(tmp_board: Path):
    ops = FileBoardOps()
    components = ops.get_components(tmp_board)
    refs = {c["reference"] for c in components}
    assert "R1" in refs
    assert "C1" in refs


def test_get_components_returns_positions(tmp_board: Path):
    ops = FileBoardOps()
    components = ops.get_components(tmp_board)
    r1 = next(c for c in components if c["reference"] == "R1")
    assert r1["position"]["x"] == pytest.approx(100.0)
    assert r1["position"]["y"] == pytest.approx(100.0)


def test_get_components_empty_board(tmp_empty_board: Path):
    ops = FileBoardOps()
    assert ops.get_components(tmp_empty_board) == []


# ---------------------------------------------------------------------------
# move_component
# ---------------------------------------------------------------------------

def test_move_component_updates_position(tmp_board: Path):
    ops = FileBoardOps()
    result = ops.move_component(tmp_board, "R1", x=55.0, y=66.0)
    assert result["position"]["x"] == pytest.approx(55.0)
    assert result["position"]["y"] == pytest.approx(66.0)

    # Verify the file was actually updated
    components = ops.get_components(tmp_board)
    r1 = next(c for c in components if c["reference"] == "R1")
    assert r1["position"]["x"] == pytest.approx(55.0)
    assert r1["position"]["y"] == pytest.approx(66.0)


def test_move_component_preserves_other_component(tmp_board: Path):
    ops = FileBoardOps()
    ops.move_component(tmp_board, "R1", x=55.0, y=66.0)
    components = ops.get_components(tmp_board)
    c1 = next(c for c in components if c["reference"] == "C1")
    # C1 must not have moved
    assert c1["position"]["x"] == pytest.approx(110.0)
    assert c1["position"]["y"] == pytest.approx(100.0)


def test_move_component_unknown_ref_raises(tmp_board: Path):
    ops = FileBoardOps()
    with pytest.raises(ValueError, match="not found"):
        ops.move_component(tmp_board, "U99", x=0.0, y=0.0)


def test_move_component_with_rotation(tmp_board: Path):
    ops = FileBoardOps()
    result = ops.move_component(tmp_board, "R1", x=10.0, y=20.0, rotation=90.0)
    assert result["rotation"] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# add_board_outline
# ---------------------------------------------------------------------------

def test_add_board_outline_writes_gr_rect(tmp_board: Path):
    ops = FileBoardOps()
    ops.add_board_outline(tmp_board, x=0.0, y=0.0, width=100.0, height=80.0)
    content = tmp_board.read_text(encoding="utf-8")
    assert "(gr_rect" in content
    assert '"Edge.Cuts"' in content


def test_add_board_outline_returns_correct_coords(tmp_board: Path):
    ops = FileBoardOps()
    result = ops.add_board_outline(tmp_board, x=5.0, y=3.0, width=100.0, height=80.0)
    assert result["x"] == pytest.approx(5.0)
    assert result["y"] == pytest.approx(3.0)
    assert result["x2"] == pytest.approx(105.0)
    assert result["y2"] == pytest.approx(83.0)


def test_add_board_outline_replaces_existing(tmp_board: Path):
    ops = FileBoardOps()
    ops.add_board_outline(tmp_board, x=0.0, y=0.0, width=100.0, height=80.0)
    ops.add_board_outline(tmp_board, x=0.0, y=0.0, width=200.0, height=150.0)
    content = tmp_board.read_text(encoding="utf-8")
    # Only one gr_rect on Edge.Cuts should remain
    assert content.count("(gr_rect") == 1
    assert "200.0" in content or "200" in content


def test_add_board_outline_on_empty_board(tmp_empty_board: Path):
    ops = FileBoardOps()
    result = ops.add_board_outline(tmp_empty_board, x=0.0, y=0.0, width=50.0, height=40.0)
    content = tmp_empty_board.read_text(encoding="utf-8")
    assert "(gr_rect" in content
    assert result["width"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# validate_board
# ---------------------------------------------------------------------------

def test_validate_board_missing_edge_cuts(tmp_board: Path):
    """Board with no Edge.Cuts should fail with an error."""
    ops = FileBoardOps()
    result = ops.validate_board(tmp_board)
    assert result["passed"] is False
    assert result["error_count"] >= 1
    types = [v["type"] for v in result["violations"]]
    assert "missing_edge_cuts" in types


def test_validate_board_passes_with_outline(tmp_board: Path):
    """Board with an Edge.Cuts gr_rect should not have missing_edge_cuts error."""
    ops = FileBoardOps()
    ops.add_board_outline(tmp_board, x=0.0, y=0.0, width=150.0, height=120.0)
    result = ops.validate_board(tmp_board)
    types = [v["type"] for v in result["violations"]]
    assert "missing_edge_cuts" not in types


def test_validate_board_duplicate_reference(tmp_path: Path):
    """Board with duplicate reference designators should flag an error."""
    import textwrap
    content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))
          (footprint "Device:R" (layer "F.Cu") (at 10 10)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
          (footprint "Device:R" (layer "F.Cu") (at 20 10)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
        )
    """)
    board = tmp_path / "dup.kicad_pcb"
    board.write_text(content, encoding="utf-8")
    ops = FileBoardOps()
    result = ops.validate_board(board)
    types = [v["type"] for v in result["violations"]]
    assert "duplicate_reference" in types


def test_validate_board_checks_performed(tmp_board: Path):
    ops = FileBoardOps()
    result = ops.validate_board(tmp_board)
    assert "edge_cuts" in result["checks_performed"]
    assert "duplicate_references" in result["checks_performed"]
    assert "zero_position" in result["checks_performed"]
    assert "design_rules" in result["checks_performed"]


# ---------------------------------------------------------------------------
# place_components_bulk
# ---------------------------------------------------------------------------

def test_place_components_bulk_single_write(tmp_empty_board: Path, fixture_footprint_dir, monkeypatch):
    """place_components_bulk should place all components and write the file once."""
    from unittest.mock import patch

    write_calls = []
    original_write = Path.write_text

    def counting_write(self, *args, **kwargs):
        write_calls.append(str(self))
        return original_write(self, *args, **kwargs)

    ops = FileBoardOps()
    components = [
        {"reference": "R1", "footprint": "Device:R", "x": 10.0, "y": 10.0},
        {"reference": "C1", "footprint": "Device:C", "x": 20.0, "y": 10.0},
    ]

    with patch.object(Path, "write_text", counting_write):
        result = ops.place_components_bulk(tmp_empty_board, components)

    # Should have written the file exactly once (regardless of component count)
    assert len(write_calls) == 1
    assert "R1" in result["placed"] or "C1" in result["placed"] or len(result["failed"]) == 2


def test_place_components_bulk_returns_placed(tmp_empty_board: Path):
    ops = FileBoardOps()
    # Use stub footprints (library not found → stub placed)
    components = [
        {"reference": "X1", "footprint": "DoesNotExist:Stub", "x": 5.0, "y": 5.0},
        {"reference": "X2", "footprint": "DoesNotExist:Stub", "x": 15.0, "y": 5.0},
    ]
    result = ops.place_components_bulk(tmp_empty_board, components)
    # Stubs are placed (no footprint lib required for stub path)
    assert set(result["placed"]) == {"X1", "X2"}
    assert result["failed"] == []


def test_place_components_bulk_missing_reference(tmp_empty_board: Path):
    ops = FileBoardOps()
    components = [{"footprint": "Device:R", "x": 5.0, "y": 5.0}]  # no reference
    result = ops.place_components_bulk(tmp_empty_board, components)
    assert len(result["failed"]) == 1


# ---------------------------------------------------------------------------
# diff_board (via FileBoardOps get_components / get_tracks)
# ---------------------------------------------------------------------------

def test_diff_board_detects_added_component(tmp_path: Path):
    """diff_board should detect a component added to board B."""
    import textwrap
    import json

    board_a_content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (footprint "Device:R" (layer "F.Cu") (at 10 10)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
        )
    """)
    board_b_content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (footprint "Device:R" (layer "F.Cu") (at 10 10)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
          (footprint "Device:C" (layer "F.Cu") (at 20 10)
            (property "Reference" "C1" (at 0 0) (layer "F.Fab"))
          )
        )
    """)
    pa = tmp_path / "a.kicad_pcb"
    pb = tmp_path / "b.kicad_pcb"
    pa.write_text(board_a_content, encoding="utf-8")
    pb.write_text(board_b_content, encoding="utf-8")

    ops = FileBoardOps()
    comps_a = {c["reference"]: c for c in ops.get_components(pa) if c.get("reference")}
    comps_b = {c["reference"]: c for c in ops.get_components(pb) if c.get("reference")}

    added = [ref for ref in comps_b if ref not in comps_a]
    assert "C1" in added
    assert "R1" not in added


def test_diff_board_detects_moved_component(tmp_path: Path):
    """diff_board should detect a component that moved position."""
    import textwrap
    import math

    board_a_content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (footprint "Device:R" (layer "F.Cu") (at 10 10)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
        )
    """)
    board_b_content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (footprint "Device:R" (layer "F.Cu") (at 50 50)
            (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
          )
        )
    """)
    pa = tmp_path / "a.kicad_pcb"
    pb = tmp_path / "b.kicad_pcb"
    pa.write_text(board_a_content, encoding="utf-8")
    pb.write_text(board_b_content, encoding="utf-8")

    ops = FileBoardOps()
    comps_a = {c["reference"]: c for c in ops.get_components(pa) if c.get("reference")}
    comps_b = {c["reference"]: c for c in ops.get_components(pb) if c.get("reference")}

    pos_a = comps_a["R1"]["position"]
    pos_b = comps_b["R1"]["position"]
    dx = pos_b.get("x", 0) - pos_a.get("x", 0)
    dy = pos_b.get("y", 0) - pos_a.get("y", 0)
    delta = math.sqrt(dx * dx + dy * dy)

    assert delta > 0.01  # should be ~56.6 mm
