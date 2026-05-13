"""Tests for place_at_edge (board.py, Phase 6.1.3)."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.file_backend import FileBoardOps


class _FileBackend(BackendProtocol):
    """Test-only BackendProtocol that returns a real FileBoardOps for both
    get_board_ops() and get_board_modify_ops(). Lets place_at_edge actually
    mutate the test fixture file so we can verify the resulting (at) clause.
    """

    def __init__(self):
        self._ops = FileBoardOps()

    def get_board_ops(self):
        return self._ops

    def get_board_modify_ops(self):
        return self._ops


def _board(
    bbox: tuple[float, float, float, float],
    *footprints: dict,
) -> str:
    lines = [
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
        f'  (gr_rect (start {bbox[0]} {bbox[1]}) (end {bbox[2]} {bbox[3]}) '
        f'(stroke (width 0.05)) (layer "Edge.Cuts"))',
    ]
    for fp in footprints:
        ref = fp["ref"]
        ax = fp["at_x"]
        ay = fp["at_y"]
        rot = fp.get("rotation", 0)
        lib_id = fp.get("lib_id", "Connector_JST:JST_PH_Horizontal")
        at_clause = f"{ax} {ay} {rot}" if rot else f"{ax} {ay}"
        cy = fp.get("courtyard", (-2.0, -1.5, 4.0, 6.5))
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


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _call_tool(board_path: Path, **kwargs) -> dict:
    import fastmcp
    from kicad_mcp.tools import board
    from kicad_mcp.utils.change_log import ChangeLog

    backend = _FileBackend()
    change_log = ChangeLog(board_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    board.register_tools(mcp, backend, change_log)
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "place_at_edge")
    return json.loads(tool_fn(str(board_path), **kwargs))


def _read_placement(board_path: Path, ref: str) -> tuple[float, float, float]:
    """Return (x, y, rotation) of the placed footprint with reference *ref*."""
    content = board_path.read_text(encoding="utf-8")
    m = re.search(
        rf'\(footprint\s+"[^"]+".*?\(property\s+"Reference"\s+"{re.escape(ref)}"',
        content, re.DOTALL,
    )
    assert m, f"footprint {ref} not found"
    # The matched range spans (footprint ...header... (at ...) ... (property "Reference" "{ref}"
    snippet = content[m.start() : m.end()]
    at_m = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)', snippet)
    assert at_m, f"could not find (at ...) for {ref} in {snippet!r}"
    return (
        float(at_m.group(1)),
        float(at_m.group(2)),
        float(at_m.group(3)) if at_m.group(3) else 0.0,
    )


# ---------------------------------------------------------------------------
# Anchoring at each edge produces outward-facing orientation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edge", ["north", "south", "east", "west"])
def test_place_at_each_edge_passes_validation(tmp_path: Path, edge):
    """After place_at_edge, validate_connector_orientations must pass."""
    board = _write(tmp_path, f"{edge}.kicad_pcb", _board(
        (0, 0, 80, 70),
        # Place initially at origin (off-board, wrong orientation) — place_at_edge fixes it
        {
            "ref": "J1", "at_x": 0.0, "at_y": 0.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),  # local +y
            "courtyard": (-2.0, -1.5, 4.0, 6.5),
        },
    ))

    result = _call_tool(board, reference="J1", edge=edge, offset_mm=2.0)
    assert result["status"] == "success"
    assert result["edge"] == edge

    # Re-validate — the new placement should pass
    from kicad_mcp.tools.drc import run_validate_connector_orientations
    val = run_validate_connector_orientations(board)
    assert val["passed"] is True, (
        f"After place_at_edge({edge}), expected validation pass; "
        f"got violations: {val['violations']}"
    )


@pytest.mark.parametrize("edge,expected_rotation", [
    ("south", 0.0),    # local +y outward at south → no rotation
    ("north", 180.0),  # local +y outward at north → flip 180
    ("east", 270.0),   # local +y outward at east → rotate -90 (270)
    ("west", 90.0),    # local +y outward at west → rotate +90
])
def test_correct_rotation_for_each_edge(tmp_path: Path, edge, expected_rotation):
    board = _write(tmp_path, f"rot_{edge}.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 40.0, "at_y": 35.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),  # local +y
            "courtyard": (-2.0, -1.5, 4.0, 6.5),
        },
    ))
    _call_tool(board, reference="J1", edge=edge)
    _, _, rotation = _read_placement(board, "J1")
    assert rotation == expected_rotation, (
        f"Edge {edge}: expected rotation {expected_rotation}, got {rotation}"
    )


# ---------------------------------------------------------------------------
# Courtyard sits offset_mm inside the edge
# ---------------------------------------------------------------------------

def test_south_edge_courtyard_offset(tmp_path: Path):
    """Footprint courtyard must be offset_mm inside the south edge after placement."""
    board = _write(tmp_path, "south_offset.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 0.0, "at_y": 0.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),  # local +y
            "courtyard": (-2.0, -1.5, 4.0, 6.5),  # extends from -1.5 to +6.5 in y
        },
    ))
    _call_tool(board, reference="J1", edge="south", offset_mm=2.0)
    x, y, rot = _read_placement(board, "J1")
    # At rotation 0, courtyard ymax (board-frame) = origin_y + 6.5
    # For south edge with offset 2.0 from board_ymax=70:
    #   origin_y + 6.5 = 70 - 2.0  →  origin_y = 61.5
    assert y == pytest.approx(61.5, abs=0.01), (
        f"Expected y=61.5 (courtyard offset 2mm inside south=70), got y={y}"
    )


def test_north_edge_courtyard_offset(tmp_path: Path):
    """Rotation 180 inverts the courtyard; origin must end up below the top edge."""
    board = _write(tmp_path, "north_offset.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "J1", "at_x": 0.0, "at_y": 0.0, "rotation": 0,
            "pcb_edge": (0.0, 3.65),
            "courtyard": (-2.0, -1.5, 4.0, 6.5),
        },
    ))
    _call_tool(board, reference="J1", edge="north", offset_mm=2.0)
    x, y, rot = _read_placement(board, "J1")
    # After rotation 180, the original (ymin=-1.5, ymax=6.5) becomes (ymin=-6.5, ymax=1.5)
    # For north edge with offset 2.0:
    #   origin_y + rotated_ymin = 0 + 2.0  →  origin_y = 2.0 - (-6.5) = 8.5
    assert y == pytest.approx(8.5, abs=0.01), (
        f"Expected y=8.5 (origin after 180° rot, north offset), got y={y}"
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_unknown_edge_returns_error(tmp_path: Path):
    board = _write(tmp_path, "bad_edge.kicad_pcb", _board(
        (0, 0, 80, 70),
        {"ref": "J1", "at_x": 10.0, "at_y": 10.0, "pcb_edge": (0.0, 3.0)},
    ))
    result = _call_tool(board, reference="J1", edge="northwest")
    assert result["status"] == "error"
    assert "edge must be one of" in result["message"]


def test_non_connector_rejected(tmp_path: Path):
    """A regular SMD resistor is not edge-facing and should be rejected."""
    board = _write(tmp_path, "smd.kicad_pcb", _board(
        (0, 0, 80, 70),
        {
            "ref": "R1", "at_x": 10.0, "at_y": 10.0,
            "lib_id": "Resistor_SMD:R_0603_1608Metric",
            "courtyard": (-1.0, -0.5, 1.0, 0.5),
            # no pcb_edge marker, name does not match heuristics
        },
    ))
    result = _call_tool(board, reference="R1", edge="south")
    assert result["status"] == "error"
    assert "not detected as an edge-facing connector" in result["message"]


def test_missing_outline_rejected(tmp_path: Path):
    """No Edge.Cuts → cannot determine edges → error."""
    content = textwrap.dedent("""\
        (kicad_pcb
          (version 20231231)
          (generator "pcbnew")
          (net 0 "")
          (footprint "Connector_JST:JST_PH_Horizontal" (layer "F.Cu") (at 0 0)
            (uuid "test-j1-uuid")
            (property "Reference" "J1" (at 0 0 0) (layer "F.Fab"))
            (fp_rect (start -2 -1.5) (end 4 6.5) (layer "F.CrtYd") (stroke (width 0.05)))
            (fp_text user "PCB edge" (at 0 3.65 0) (layer "Dwgs.User"))
          )
        )
    """)
    board = _write(tmp_path, "no_outline.kicad_pcb", content)
    result = _call_tool(board, reference="J1", edge="south")
    assert result["status"] == "error"
    assert "No Edge.Cuts" in result["message"]
