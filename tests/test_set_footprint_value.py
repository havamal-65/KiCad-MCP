"""Tests for set_footprint_value on both board backends (#2/#7).

sync_schematic_to_pcb routes a footprint Value update through board_modify_ops
so the value lands on the live bridge board (not just the file behind the
bridge's back — the #7 revert root cause). Both backends implement the same
contract:

- FileBoardOps.set_footprint_value  — edits the .kicad_pcb footprint block.
- PluginBoardOps.set_footprint_value — TCP call to the bridge handler.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.backends.plugin_backend import PluginBoardOps


_PCB = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (footprint "Test:R_0603"
        (layer "F.Cu")
        (at 50 60 90)
        (uuid "aaaaaaaa-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
        (property "Value" "R_0603" (at 0 1.5 0) (layer "F.Fab"))
        (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu"))
      )
      (footprint "Test:C_0402"
        (layer "F.Cu")
        (at 70 60 0)
        (uuid "bbbbbbbb-1111-1111-1111-111111111111")
        (property "Reference" "U6" (at 0 -1.5 0) (layer "F.SilkS"))
        (property "Value" "C_0402" (at 0 1.5 0) (layer "F.Fab"))
        (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu"))
      )
    )
""")


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_PCB, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# FileBoardOps.set_footprint_value
# ---------------------------------------------------------------------------

def test_file_sets_value_for_correct_footprint(board: Path):
    result = FileBoardOps().set_footprint_value(board, "U6", "Adafruit_PMSA003I")

    assert result == {
        "reference": "U6",
        "old_value": "C_0402",
        "new_value": "Adafruit_PMSA003I",
    }
    content = board.read_text(encoding="utf-8")
    # The targeted footprint's Value changed...
    assert '(property "Value" "Adafruit_PMSA003I"' in content
    # ...and the other footprint's Value is untouched.
    assert '(property "Value" "R_0603"' in content


def test_file_raises_when_reference_missing(board: Path):
    with pytest.raises(ValueError, match="not found"):
        FileBoardOps().set_footprint_value(board, "R99", "x")


def test_file_value_change_is_idempotent(board: Path):
    ops = FileBoardOps()
    ops.set_footprint_value(board, "R1", "1k")
    ops.set_footprint_value(board, "R1", "1k")
    content = board.read_text(encoding="utf-8")
    assert content.count('(property "Value" "1k"') == 1


# ---------------------------------------------------------------------------
# PluginBoardOps.set_footprint_value — TCP routing to the bridge
# ---------------------------------------------------------------------------

def test_plugin_routes_to_bridge_handler():
    calls = []

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        calls.append((method, kwargs))
        return {"status": "ok", "reference": kwargs["reference"],
                "old_value": "C_0402", "new_value": kwargs["value"]}

    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        result = PluginBoardOps().set_footprint_value(
            Path("/tmp/b.kicad_pcb"), "U6", "Adafruit_PMSA003I",
        )

    assert len(calls) == 1
    method, kwargs = calls[0]
    assert method == "set_footprint_value"
    assert kwargs["reference"] == "U6"
    assert kwargs["value"] == "Adafruit_PMSA003I"
    assert kwargs["path"] == str(Path("/tmp/b.kicad_pcb"))
    assert result["new_value"] == "Adafruit_PMSA003I"
