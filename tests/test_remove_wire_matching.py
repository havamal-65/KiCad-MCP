"""Tests for remove_wire endpoint matching (#6).

The 2026-06 session hit ``Wire ... not found`` for a wire that provably
existed: the finder only matched the compact one-line form this tool writes,
never KiCad's native multi-line form, and never tried reversed endpoint
order. These tests pin down the lenient matcher and the nearest-wire
no-match diagnostics.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import fastmcp
import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.utils.sexp_parser import (
    find_nearest_wires,
    find_wire_block_by_endpoints,
)


# Compact one-line form, as written by add_wire.
_SCH_COMPACT = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (wire (pts (xy 63.5 87.63) (xy 71.12 87.63))
        (stroke (width 0) (type default))
        (uuid "11111111-1111-1111-1111-111111111111")
      )
      (wire (pts (xy 100 50) (xy 100 60))
        (stroke (width 0) (type default))
        (uuid "22222222-2222-2222-2222-222222222222")
      )
    )
""")

# KiCad's native multi-line form (tabs, keyword alone on its line).
_SCH_NATIVE = textwrap.dedent("""\
    (kicad_sch
    \t(version 20231120)
    \t(generator "eeschema")
    \t(paper "A4")
    \t(wire
    \t\t(pts
    \t\t\t(xy 63.5 87.63) (xy 71.12 87.63)
    \t\t)
    \t\t(stroke
    \t\t\t(width 0)
    \t\t\t(type default)
    \t\t)
    \t\t(uuid "11111111-1111-1111-1111-111111111111")
    \t)
    )
""")


# ---------------------------------------------------------------------------
# Finder — formats and orderings
# ---------------------------------------------------------------------------

def test_matches_compact_one_line_form():
    assert find_wire_block_by_endpoints(_SCH_COMPACT, 63.5, 87.63, 71.12, 87.63) is not None


def test_matches_native_multi_line_form():
    location = find_wire_block_by_endpoints(_SCH_NATIVE, 63.5, 87.63, 71.12, 87.63)
    assert location is not None
    start, end = location
    assert _SCH_NATIVE[start:start + 5] == "(wire"
    assert _SCH_NATIVE[end] == ")"


def test_matches_reversed_endpoint_order():
    assert find_wire_block_by_endpoints(_SCH_NATIVE, 71.12, 87.63, 63.5, 87.63) is not None


def test_matches_float_format_variants():
    # 63.50 vs 63.5, 71.120 vs 71.12 — numeric, not string, comparison.
    assert find_wire_block_by_endpoints(_SCH_NATIVE, 63.50, 87.630, 71.120, 87.63) is not None


def test_within_tolerance_matches_beyond_does_not():
    assert find_wire_block_by_endpoints(_SCH_NATIVE, 63.505, 87.63, 71.12, 87.63) is not None
    assert find_wire_block_by_endpoints(_SCH_NATIVE, 63.52, 87.63, 71.12, 87.63) is None


def test_no_false_match_on_other_keywords():
    content = '(kicad_sch (wires_fake (pts (xy 1 2) (xy 3 4))))'
    assert find_wire_block_by_endpoints(content, 1, 2, 3, 4) is None


# ---------------------------------------------------------------------------
# Nearest-wire ranking
# ---------------------------------------------------------------------------

def test_find_nearest_wires_sorts_by_distance():
    nearest = find_nearest_wires(_SCH_COMPACT, 64.0, 87.63, 71.0, 87.63)
    assert len(nearest) == 2
    assert nearest[0]["start"] == {"x": 63.5, "y": 87.63}
    assert nearest[0]["distance"] < nearest[1]["distance"]


def test_find_nearest_wires_uses_best_endpoint_pairing():
    # Query in reversed order — distance must still be the small pairing.
    nearest = find_nearest_wires(_SCH_COMPACT, 71.12, 87.63, 63.5, 87.63, count=1)
    assert nearest[0]["distance"] == 0.0


def test_find_nearest_wires_empty_schematic():
    assert find_nearest_wires("(kicad_sch)", 0, 0, 1, 1) == []


# ---------------------------------------------------------------------------
# remove_wire — backend + tool diagnostics
# ---------------------------------------------------------------------------

def test_remove_wire_native_form(tmp_path: Path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_SCH_NATIVE, encoding="utf-8")

    result = FileSchematicOps().remove_wire(sch, 71.12, 87.63, 63.5, 87.63)

    assert result["removed"] is True
    content = sch.read_text(encoding="utf-8")
    assert "(wire" not in content
    assert content.count("(") == content.count(")")


def test_remove_wire_no_match_lists_nearest(tmp_path: Path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_SCH_COMPACT, encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        FileSchematicOps().remove_wire(sch, 10, 10, 20, 20)

    message = str(exc_info.value)
    assert "Nearest wires" in message
    assert "(63.5, 87.63)" in message
    assert " mm" in message
    # File untouched on failure
    assert sch.read_text(encoding="utf-8") == _SCH_COMPACT


def test_remove_wire_no_match_empty_schematic(tmp_path: Path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contains no wires"):
        FileSchematicOps().remove_wire(sch, 10, 10, 20, 20)


def test_tool_surfaces_diagnostics(tmp_path: Path):
    from kicad_mcp.tools import schematic
    from kicad_mcp.utils.change_log import ChangeLog
    from kicad_mcp.backends.base import BackendProtocol

    class _Backend(BackendProtocol):
        def get_schematic_ops(self):
            return FileSchematicOps()

        def get_schematic_modify_ops(self):
            return FileSchematicOps()

    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_SCH_COMPACT, encoding="utf-8")

    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, _Backend(), ChangeLog(tmp_path / "changes.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "remove_wire"
    )

    result = json.loads(tool_fn(str(sch), 10, 10, 20, 20))
    assert result["status"] == "error"
    assert "Nearest wires" in result["message"]
