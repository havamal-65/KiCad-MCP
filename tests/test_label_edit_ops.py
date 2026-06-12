"""Tests for remove_label / set_label_text (#4).

Re-netting an existing pin (rename or remove its net label) was impossible
via MCP during the 2026-06 session and forced direct .kicad_sch edits.
These tests cover the positional finder, both FileSchematicOps methods,
and the MCP tool surface, including nearest-label no-match diagnostics.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import fastmcp
import pytest

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.utils.sexp_parser import (
    find_label_block_by_position,
    find_nearest_labels,
)


# Native KiCad 9 multi-line format: one of each kind, plus two labels close
# together to exercise text disambiguation.
_SCH = textwrap.dedent("""\
    (kicad_sch
    \t(version 20231120)
    \t(generator "eeschema")
    \t(paper "A4")
    \t(label "I2C_SDA"
    \t\t(at 96.52 73.66 0)
    \t\t(effects
    \t\t\t(font
    \t\t\t\t(size 1.27 1.27)
    \t\t\t)
    \t\t\t(justify left bottom)
    \t\t)
    \t\t(uuid "11111111-1111-1111-1111-111111111111")
    \t)
    \t(label "I2C_SCL"
    \t\t(at 96.52 76.2 0)
    \t\t(uuid "22222222-2222-2222-2222-222222222222")
    \t)
    \t(global_label "USB_5V"
    \t\t(shape input)
    \t\t(at 50.8 45.72 180)
    \t\t(uuid "33333333-3333-3333-3333-333333333333")
    \t)
    \t(hierarchical_label "EN"
    \t\t(shape input)
    \t\t(at 30 30 0)
    \t\t(uuid "44444444-4444-4444-4444-444444444444")
    \t)
    )
""")


def _write(tmp_path: Path) -> Path:
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    return sch


# ---------------------------------------------------------------------------
# Finder
# ---------------------------------------------------------------------------

def test_finds_local_label_by_position():
    location = find_label_block_by_position(_SCH, 96.52, 73.66)
    assert location is not None
    start, end = location
    assert _SCH[start:].startswith('(label "I2C_SDA"')
    assert _SCH[end] == ")"


def test_finds_global_and_hierarchical_labels():
    assert find_label_block_by_position(_SCH, 50.8, 45.72) is not None
    assert find_label_block_by_position(_SCH, 30, 30) is not None


def test_text_filter_disambiguates():
    location = find_label_block_by_position(_SCH, 96.52, 73.66, text="I2C_SDA")
    assert location is not None
    assert find_label_block_by_position(_SCH, 96.52, 73.66, text="I2C_SCL") is None


def test_tolerance_and_float_formats():
    assert find_label_block_by_position(_SCH, 96.520, 73.66) is not None
    assert find_label_block_by_position(_SCH, 96.525, 73.66) is not None
    assert find_label_block_by_position(_SCH, 96.6, 73.66) is None


def test_kinds_filter():
    assert find_label_block_by_position(_SCH, 50.8, 45.72, kinds=("label",)) is None


def test_find_nearest_labels_sorted():
    nearest = find_nearest_labels(_SCH, 96.52, 74.0)
    assert nearest[0]["text"] == "I2C_SDA"
    assert nearest[1]["text"] == "I2C_SCL"
    assert nearest[0]["distance"] < nearest[1]["distance"]
    assert len(nearest) == 3


# ---------------------------------------------------------------------------
# remove_label
# ---------------------------------------------------------------------------

def test_remove_label_deletes_only_target(tmp_path: Path):
    sch = _write(tmp_path)

    result = FileSchematicOps().remove_label(sch, 96.52, 73.66)

    assert result["removed"] is True
    assert result["text"] == "I2C_SDA"
    assert result["label_type"] == "label"
    content = sch.read_text(encoding="utf-8")
    assert "I2C_SDA" not in content
    assert "I2C_SCL" in content
    assert "USB_5V" in content
    assert content.count("(") == content.count(")")


def test_remove_label_with_text_filter(tmp_path: Path):
    sch = _write(tmp_path)

    result = FileSchematicOps().remove_label(sch, 96.52, 76.2, text="I2C_SCL")

    assert result["text"] == "I2C_SCL"
    content = sch.read_text(encoding="utf-8")
    assert "I2C_SCL" not in content
    assert "I2C_SDA" in content


def test_remove_label_no_match_lists_nearest(tmp_path: Path):
    sch = _write(tmp_path)

    with pytest.raises(ValueError) as exc_info:
        FileSchematicOps().remove_label(sch, 96.52, 74.5)

    message = str(exc_info.value)
    assert "Nearest labels" in message
    assert "I2C_SDA" in message
    assert " mm away" in message
    assert sch.read_text(encoding="utf-8") == _SCH


def test_remove_label_empty_schematic(tmp_path: Path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contains no labels"):
        FileSchematicOps().remove_label(sch, 10, 10)


# ---------------------------------------------------------------------------
# set_label_text
# ---------------------------------------------------------------------------

def test_set_label_text_renames_in_place(tmp_path: Path):
    sch = _write(tmp_path)

    result = FileSchematicOps().set_label_text(sch, 50.8, 45.72, "XIAO_5V")

    assert result == {
        "old_text": "USB_5V",
        "new_text": "XIAO_5V",
        "label_type": "global_label",
        "position": {"x": 50.8, "y": 45.72},
    }
    content = sch.read_text(encoding="utf-8")
    assert '(global_label "XIAO_5V"' in content
    assert "USB_5V" not in content
    # Block structure untouched: position, shape, and uuid survive
    assert "(at 50.8 45.72 180)" in content
    assert "(shape input)" in content
    assert "33333333-3333-3333-3333-333333333333" in content
    assert content.count("(") == content.count(")")


def test_set_label_text_old_text_filter(tmp_path: Path):
    sch = _write(tmp_path)

    with pytest.raises(ValueError, match="Nearest labels"):
        FileSchematicOps().set_label_text(
            sch, 96.52, 73.66, "NEW", old_text="WRONG_OLD",
        )
    assert sch.read_text(encoding="utf-8") == _SCH


def test_set_label_text_escapes_quotes(tmp_path: Path):
    sch = _write(tmp_path)

    FileSchematicOps().set_label_text(sch, 30, 30, 'EN_"FAST"')

    content = sch.read_text(encoding="utf-8")
    assert '(hierarchical_label "EN_\\"FAST\\""' in content
    # And the renamed label is still findable by its (escaped) text
    assert find_label_block_by_position(content, 30, 30, text='EN_"FAST"') is not None


def test_add_set_remove_round_trip(tmp_path: Path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch\n  (version 20231120)\n)\n", encoding="utf-8")
    ops = FileSchematicOps()

    ops.add_label(sch, "VBAT", 25.4, 25.4)
    renamed = ops.set_label_text(sch, 25.4, 25.4, "VSYS", old_text="VBAT")
    assert renamed["old_text"] == "VBAT"

    removed = ops.remove_label(sch, 25.4, 25.4, text="VSYS")
    assert removed["removed"] is True
    content = sch.read_text(encoding="utf-8")
    assert "VSYS" not in content
    assert content.count("(") == content.count(")")


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

class _Backend(BackendProtocol):
    def get_schematic_ops(self):
        return FileSchematicOps()

    def get_schematic_modify_ops(self):
        return FileSchematicOps()


def _tool(tmp_path: Path, name: str):
    from kicad_mcp.tools import schematic
    from kicad_mcp.utils.change_log import ChangeLog

    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, _Backend(), ChangeLog(tmp_path / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == name)


def test_tool_remove_label(tmp_path: Path):
    sch = _write(tmp_path)

    result = json.loads(_tool(tmp_path, "remove_label")(str(sch), 96.52, 73.66))

    assert result["status"] == "success"
    assert result["text"] == "I2C_SDA"
    assert "I2C_SDA" not in sch.read_text(encoding="utf-8")


def test_tool_set_label_text(tmp_path: Path):
    sch = _write(tmp_path)

    result = json.loads(
        _tool(tmp_path, "set_label_text")(str(sch), 50.8, 45.72, "XIAO_5V")
    )

    assert result["status"] == "success"
    assert result["old_text"] == "USB_5V"
    assert '"XIAO_5V"' in sch.read_text(encoding="utf-8")


def test_tool_no_match_returns_error_json(tmp_path: Path):
    sch = _write(tmp_path)

    result = json.loads(_tool(tmp_path, "remove_label")(str(sch), 1, 1))

    assert result["status"] == "error"
    assert "Nearest labels" in result["message"]
