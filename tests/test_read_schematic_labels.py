"""Tests for read_schematic label fallback (#5).

The kicad-skip read path returned ``num_labels: 0`` on fully label-netted
schematics during the 2026-06 session. The fix: when the skip path raises
or yields zero labels while the raw file plainly contains label blocks,
fall back to the sexp label parse.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps


# Native KiCad 9 multi-line format with one of each label kind.
_SCH_LABELED = textwrap.dedent("""\
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
    \t(global_label "USB_5V"
    \t\t(shape input)
    \t\t(at 50.8 45.72 180)
    \t\t(uuid "22222222-2222-2222-2222-222222222222")
    \t)
    \t(hierarchical_label "EN"
    \t\t(shape input)
    \t\t(at 30 30 0)
    \t\t(uuid "33333333-3333-3333-3333-333333333333")
    \t)
    )
""")

_SCH_NO_LABELS = textwrap.dedent("""\
    (kicad_sch
    \t(version 20231120)
    \t(generator "eeschema")
    \t(paper "A4")
    )
""")


class _EmptySkipSch:
    """Simulates kicad-skip silently exposing no labels (the #5 symptom)."""


class _RaisingSkipSch:
    """Simulates kicad-skip blowing up on label access."""

    @property
    def label(self):
        raise RuntimeError("skip exploded on labels")


class _SkipLabel:
    text = "NET_FROM_SKIP"


class _SkipSchWithLabels:
    label = [_SkipLabel()]


def _write(tmp_path: Path, content: str) -> Path:
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(content, encoding="utf-8")
    return sch


def test_fallback_when_skip_yields_zero_labels(tmp_path: Path, caplog):
    sch = _write(tmp_path, _SCH_LABELED)

    with caplog.at_level("WARNING"):
        result = FileSchematicOps()._read_with_skip(_EmptySkipSch(), sch)

    assert result["info"]["num_labels"] == 3
    by_text = {lbl["text"]: lbl for lbl in result["labels"]}
    assert by_text["I2C_SDA"]["label_type"] == "label"
    assert by_text["I2C_SDA"]["position"] == {"x": 96.52, "y": 73.66}
    assert by_text["USB_5V"]["label_type"] == "global_label"
    assert by_text["EN"]["label_type"] == "hierarchical_label"
    assert "falling back to sexp label parse" in caplog.text


def test_fallback_when_skip_label_access_raises(tmp_path: Path, caplog):
    sch = _write(tmp_path, _SCH_LABELED)

    with caplog.at_level("WARNING"):
        result = FileSchematicOps()._read_with_skip(_RaisingSkipSch(), sch)

    assert result["info"]["num_labels"] == 3
    assert "skip exploded on labels" in caplog.text


def test_no_fallback_when_file_has_no_labels(tmp_path: Path, caplog):
    sch = _write(tmp_path, _SCH_NO_LABELS)

    with caplog.at_level("WARNING"):
        result = FileSchematicOps()._read_with_skip(_EmptySkipSch(), sch)

    assert result["info"]["num_labels"] == 0
    assert result["labels"] == []
    assert "falling back" not in caplog.text


def test_skip_labels_kept_when_skip_works(tmp_path: Path):
    # Skip found a label — its result wins; no fallback overwrite.
    sch = _write(tmp_path, _SCH_LABELED)

    result = FileSchematicOps()._read_with_skip(_SkipSchWithLabels(), sch)

    assert result["info"]["num_labels"] == 1
    assert result["labels"][0]["text"] == "NET_FROM_SKIP"


def test_read_schematic_end_to_end_sees_labels(tmp_path: Path):
    # The user-visible symptom: whatever parse path runs, labels must appear.
    sch = _write(tmp_path, _SCH_LABELED)

    result = FileSchematicOps().read_schematic(sch)

    assert result["info"]["num_labels"] == 3
    assert {lbl["text"] for lbl in result["labels"]} == {"I2C_SDA", "USB_5V", "EN"}
