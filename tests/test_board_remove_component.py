"""Unit tests for board-side remove_component (#3 file side).

FileBoardOps.remove_component must remove the footprint block AND return the
captured placement state (footprint lib_id, position, rotation, layer, locked,
pad→net map) — that payload is what sync_schematic_to_pcb's footprint-swap
path (#2) consumes.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps, _footprint_state_from_block


_PCB_TWO_FOOTPRINTS = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "VCC")
      (net 2 "GND")
      (footprint "Resistor_SMD:R_0603_1608Metric"
        (layer "F.Cu")
        (at 50 60 90)
        (uuid "aaaaaaaa-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 -1.5 0)
          (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (property "Value" "10k" (at 0 1.5 0)
          (layer "F.Fab")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask")
          (net 1 "VCC")
        )
        (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask")
          (net 2 "GND")
        )
      )
      (footprint "Capacitor_SMD:C_0603_1608Metric"
        (layer "B.Cu")
        (at 70 60)
        (uuid "bbbbbbbb-2222-2222-2222-222222222222")
        (property "Reference" "C1" (at 0 -1.5 0)
          (layer "B.SilkS")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (property "Value" "100n" (at 0 1.5 0)
          (layer "B.Fab")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (pad "1" smd roundrect (at -0.775 0) (size 0.9 0.95) (layers "B.Cu" "B.Paste" "B.Mask")
          (net 1 "VCC")
        )
        (pad "2" smd roundrect (at 0.775 0) (size 0.9 0.95) (layers "B.Cu" "B.Paste" "B.Mask"))
      )
    )
""")


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_pcb"
    p.write_text(_PCB_TWO_FOOTPRINTS, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# remove_component
# ---------------------------------------------------------------------------

def test_remove_component_removes_block_and_returns_state(board: Path):
    result = FileBoardOps().remove_component(board, "R1")

    assert result["reference"] == "R1"
    assert result["removed"] is True
    assert result["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert result["position"] == {"x": 50.0, "y": 60.0}
    assert result["rotation"] == 90.0
    assert result["layer"] == "F.Cu"
    assert result["locked"] is False
    assert result["pad_nets"] == {"1": "VCC", "2": "GND"}

    content = board.read_text(encoding="utf-8")
    assert '"R1"' not in content
    assert "R_0603_1608Metric" not in content
    # The other footprint is untouched
    assert '"C1"' in content
    assert "C_0603_1608Metric" in content


def test_remove_component_other_footprint_state(board: Path):
    """No rotation clause → 0.0; pad without a net is omitted from pad_nets."""
    result = FileBoardOps().remove_component(board, "C1")

    assert result["rotation"] == 0.0
    assert result["layer"] == "B.Cu"
    assert result["pad_nets"] == {"1": "VCC"}  # pad 2 has no net clause

    content = board.read_text(encoding="utf-8")
    assert '"C1"' not in content
    assert '"R1"' in content


def test_remove_component_not_found_raises(board: Path):
    with pytest.raises(ValueError, match="U99"):
        FileBoardOps().remove_component(board, "U99")
    # File unchanged on failure
    assert board.read_text(encoding="utf-8") == _PCB_TWO_FOOTPRINTS


def test_remove_component_file_still_balanced(board: Path):
    FileBoardOps().remove_component(board, "R1")
    content = board.read_text(encoding="utf-8")
    assert content.count("(") == content.count(")")


# ---------------------------------------------------------------------------
# get_component_state (non-destructive capture)
# ---------------------------------------------------------------------------

def test_get_component_state_does_not_modify(board: Path):
    state = FileBoardOps().get_component_state(board, "R1")

    assert state["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert state["position"] == {"x": 50.0, "y": 60.0}
    assert state["pad_nets"] == {"1": "VCC", "2": "GND"}
    assert "removed" not in state
    assert board.read_text(encoding="utf-8") == _PCB_TWO_FOOTPRINTS


def test_get_component_state_not_found_raises(board: Path):
    with pytest.raises(ValueError, match="U99"):
        FileBoardOps().get_component_state(board, "U99")


# ---------------------------------------------------------------------------
# _footprint_state_from_block edge cases
# ---------------------------------------------------------------------------

def test_state_locked_kicad9_style():
    block = (
        '(footprint "Lib:FP"\n'
        '  (locked yes)\n'
        '  (layer "F.Cu")\n'
        '  (at 10 20)\n'
        ')'
    )
    assert _footprint_state_from_block(block)["locked"] is True


def test_state_locked_legacy_bare_token():
    block = '(footprint "Lib:FP" locked (layer "F.Cu") (at 10 20))'
    assert _footprint_state_from_block(block)["locked"] is True


def test_state_unquoted_pad_name():
    block = (
        '(footprint "Lib:FP"\n'
        '  (layer "F.Cu")\n'
        '  (at 0 0)\n'
        '  (pad 1 thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1.0)\n'
        '    (net 3 "SIG")\n'
        '  )\n'
        ')'
    )
    assert _footprint_state_from_block(block)["pad_nets"] == {"1": "SIG"}


def test_state_multi_pad_same_name_keeps_net():
    """Thermal-pad arrays repeat one logical pad name across physical holes."""
    block = (
        '(footprint "Lib:FP"\n'
        '  (layer "F.Cu")\n'
        '  (at 0 0)\n'
        '  (pad "9" smd rect (at 0 0) (size 1 1) (net 2 "GND"))\n'
        '  (pad "9" smd rect (at 1 0) (size 1 1) (net 2 "GND"))\n'
        ')'
    )
    assert _footprint_state_from_block(block)["pad_nets"] == {"9": "GND"}
