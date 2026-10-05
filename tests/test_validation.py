"""Input-validation regressions (utils/validation.py).

The reference pattern must accept everything KiCad itself writes as a
reference designator — including underscored refs like ``T14_R1`` (authored
onto the integration scratch board by KiCad) and multi-unit suffixes — while
still rejecting obvious junk. Found live 2026-07-02: the old letters+digits
pattern refused ``move_component`` on the scratch board's own refs.
"""

from __future__ import annotations

import pytest

from kicad_mcp.models.errors import InvalidReferenceError
from kicad_mcp.utils.validation import validate_reference


@pytest.mark.parametrize("ref", [
    "R1", "U3", "C10", "Q2A", "U3B",          # classic + multi-unit
    "#PWR001", "#FLG02",                       # power/flag symbols
    "T14_R1", "T10_U1", "SW_1", "IC2_A",       # underscored (KiCad-legal)
    "R1_2",                                    # trailing underscore segment
])
def test_valid_references_accepted(ref: str) -> None:
    assert validate_reference(ref) == ref


@pytest.mark.parametrize("ref", [
    "", "R", "1R", "_R1", "#", "#PWR",         # no digit / bad start
    "R 1", "R1;", "REF**", "R-1", "U.1",       # whitespace / punctuation
])
def test_invalid_references_rejected(ref: str) -> None:
    with pytest.raises(InvalidReferenceError):
        validate_reference(ref)


# ---------------------------------------------------------------------------
# Net names (RW-S4 §2 / F-REC2) — KiCad's real grammar
# ---------------------------------------------------------------------------

from kicad_mcp.models.errors import InvalidNetNameError  # noqa: E402
from kicad_mcp.utils.validation import validate_net_name  # noqa: E402


@pytest.mark.parametrize("name", [
    "Net-(A-B)",            # kicad-cli-measured auto name
    "BUS[3]",               # bus member
    "A B",                  # interior space
    "N{X}",                 # brace group
    "Net-(X1-Pad2)",        # the ecc83-recreation tripwire
])
def test_net_name_accepts_kicad_auto_names(name: str) -> None:
    assert validate_net_name(name) == name


@pytest.mark.parametrize("name", [" VCC", "VCC ", " Net-(A-B) ", "\tGND", "GND\n"])
def test_net_name_rejects_edge_whitespace(name: str) -> None:
    with pytest.raises(InvalidNetNameError, match="whitespace"):
        validate_net_name(name)


@pytest.mark.parametrize("name", ['NET"X"', "A\nB", "A\tB", "NET;X", "NET|X"])
def test_net_name_rejects_quotes_and_control(name: str) -> None:
    with pytest.raises(InvalidNetNameError):
        validate_net_name(name)


@pytest.mark.parametrize("name", [
    "VCC", "GND", "/sheet1/SDA", "USB_D+", "NET-1", "a.b~c",
])
def test_net_name_existing_accepts_unchanged(name: str) -> None:
    assert validate_net_name(name) == name
