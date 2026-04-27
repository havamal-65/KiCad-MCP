"""Shared pytest fixtures for the KiCad MCP test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal KiCad file content helpers
# ---------------------------------------------------------------------------

MINIMAL_PCB = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "GND")
      (net 2 "VCC")
      (footprint "Device:R" (layer "F.Cu") (at 100 100)
        (property "Reference" "R1" (at 0 -1.65 0) (layer "F.Fab"))
        (property "Value" "10k" (at 0 1.65 0) (layer "F.Fab"))
        (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 0 0) (layer "F.Fab") (hide yes))
        (pad 1 smd roundrect (at -0.9125 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
        (pad 2 smd roundrect (at 0.9125 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VCC"))
      )
      (footprint "Device:C" (layer "F.Cu") (at 110 100)
        (property "Reference" "C1" (at 0 -1.65 0) (layer "F.Fab"))
        (property "Value" "100nF" (at 0 1.65 0) (layer "F.Fab"))
        (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 0 0) (layer "F.Fab") (hide yes))
        (pad 1 smd roundrect (at -0.9125 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
        (pad 2 smd roundrect (at 0.9125 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VCC"))
      )
    )
""")

MINIMAL_PCB_NO_COMPONENTS = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
    )
""")


@pytest.fixture
def tmp_board(tmp_path: Path) -> Path:
    """A minimal .kicad_pcb file with two components (R1, C1)."""
    board_file = tmp_path / "test_board.kicad_pcb"
    board_file.write_text(MINIMAL_PCB, encoding="utf-8")
    return board_file


@pytest.fixture
def tmp_empty_board(tmp_path: Path) -> Path:
    """A minimal .kicad_pcb file with no components."""
    board_file = tmp_path / "empty_board.kicad_pcb"
    board_file.write_text(MINIMAL_PCB_NO_COMPONENTS, encoding="utf-8")
    return board_file


@pytest.fixture
def fixture_footprint_dir() -> Path:
    """Path to the test fixtures footprint directory."""
    return Path(__file__).parent / "fixtures" / "footprints"
