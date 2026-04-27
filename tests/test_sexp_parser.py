"""Tests for the S-expression parser utilities."""

from __future__ import annotations

import textwrap

import pytest

from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference


# ---------------------------------------------------------------------------
# find_footprint_block_by_reference
# ---------------------------------------------------------------------------

PCB_WITH_TWO_COMPONENTS = textwrap.dedent("""\
    (kicad_pcb
      (footprint "Device:R" (layer "F.Cu") (at 100 100)
        (property "Reference" "R1" (at 0 -1.65 0) (layer "F.Fab"))
        (property "Value" "10k" (at 0 1.65 0) (layer "F.Fab"))
      )
      (footprint "Device:C" (layer "F.Cu") (at 110 100)
        (property "Reference" "C1" (at 0 -1.65 0) (layer "F.Fab"))
        (property "Value" "100nF" (at 0 1.65 0) (layer "F.Fab"))
      )
    )
""")

PCB_WITH_FP_TEXT_FORMAT = textwrap.dedent("""\
    (kicad_pcb
      (footprint "Device:R" (layer "F.Cu") (at 50 50)
        (fp_text reference "U1" (at 0 0) (layer "F.Fab"))
        (fp_text value "ATmega328P" (at 0 2) (layer "F.Fab"))
      )
    )
""")

PCB_WITH_NESTED_PARENS_IN_VALUE = textwrap.dedent("""\
    (kicad_pcb
      (footprint "Device:R" (layer "F.Cu") (at 20 20)
        (property "Reference" "R2" (at 0 -1.65 0) (layer "F.Fab"))
        (property "Value" "(10k±1%)" (at 0 1.65 0) (layer "F.Fab"))
      )
    )
""")


def test_find_known_reference_returns_span():
    result = find_footprint_block_by_reference(PCB_WITH_TWO_COMPONENTS, "R1")
    assert result is not None
    start, end = result
    block = PCB_WITH_TWO_COMPONENTS[start:end + 1]
    assert block.startswith("(footprint")
    assert '"R1"' in block


def test_find_second_component():
    result = find_footprint_block_by_reference(PCB_WITH_TWO_COMPONENTS, "C1")
    assert result is not None
    start, end = result
    block = PCB_WITH_TWO_COMPONENTS[start:end + 1]
    assert '"C1"' in block
    # Must NOT include R1
    assert '"R1"' not in block


def test_find_unknown_reference_returns_none():
    result = find_footprint_block_by_reference(PCB_WITH_TWO_COMPONENTS, "U99")
    assert result is None


def test_find_fp_text_reference_format():
    """Older KiCad format uses (fp_text reference ...) instead of (property "Reference" ...)."""
    result = find_footprint_block_by_reference(PCB_WITH_FP_TEXT_FORMAT, "U1")
    assert result is not None
    block = PCB_WITH_FP_TEXT_FORMAT[result[0]:result[1] + 1]
    assert "U1" in block


def test_find_component_with_nested_parens_in_value():
    """Nested parens in a property value must not confuse the paren-walker."""
    result = find_footprint_block_by_reference(PCB_WITH_NESTED_PARENS_IN_VALUE, "R2")
    assert result is not None


def test_find_returns_none_on_empty_string():
    assert find_footprint_block_by_reference("", "R1") is None


def test_find_returns_none_on_no_footprints():
    content = "(kicad_pcb (version 20231231))"
    assert find_footprint_block_by_reference(content, "R1") is None


def test_returned_span_covers_full_balanced_block():
    result = find_footprint_block_by_reference(PCB_WITH_TWO_COMPONENTS, "R1")
    assert result is not None
    start, end = result
    block = PCB_WITH_TWO_COMPONENTS[start:end + 1]
    # The extracted block must be balanced (equal open/close parens).
    assert block.count("(") == block.count(")")
