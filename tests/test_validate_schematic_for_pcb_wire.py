"""REQ-TEST-004 — §6.2 Check 7 wired into run_validate_schematic_for_pcb.

A symbol-footprint pad mismatch must surface as a blocking issue in
run_validate_schematic_for_pcb (so ready_for_pcb_sync flips False), proving the
validator and the schematic-readiness gate work together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.drc import run_validate_schematic_for_pcb

_SCH_DIR = Path(__file__).parent / "fixtures" / "schematics"
_FP_DIR = Path(__file__).parent / "fixtures" / "footprints"
_SYM_DIR = Path(__file__).parent / "fixtures" / "symbols"


@pytest.fixture(autouse=True)
def _fixture_libraries(monkeypatch):
    monkeypatch.setattr(
        "kicad_mcp.utils.fp_lib_table.get_footprint_library_map",
        lambda project_dir=None: {},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_paths.find_footprint_libraries",
        lambda project_dir=None: [_FP_DIR / "TestPkg"],
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_paths.find_symbol_libraries",
        lambda: sorted(_SYM_DIR.glob("*.kicad_sym")),
    )


def test_validator_mismatch_blocks_sync_ready():
    result = run_validate_schematic_for_pcb(_SCH_DIR / "pin_missing.kicad_sch")

    assert result["ready_for_pcb_sync"] is False, result
    pad_issues = [
        b for b in result["blocking_issues"]
        if b.get("type") == "footprint_pad_mismatch"
    ]
    assert len(pad_issues) == 1, result["blocking_issues"]
    issue = pad_issues[0]
    assert issue["reference"] == "U1"
    assert "['5']" in issue["detail"]  # the missing pad is named in the detail


def test_extra_pads_are_warning_not_blocking():
    """Extra footprint pads (thermal/mech/NC) surface as a warning, not blocking."""
    result = run_validate_schematic_for_pcb(_SCH_DIR / "extra_pads.kicad_sch")

    pad_blocks = [
        b for b in result["blocking_issues"]
        if b.get("type") == "footprint_pad_mismatch"
    ]
    assert pad_blocks == [], result["blocking_issues"]
    extra_warnings = [
        w for w in result["warnings"]
        if w.get("type") == "extra_footprint_pads"
    ]
    assert len(extra_warnings) == 1, result["warnings"]
    assert extra_warnings[0]["reference"] == "U1"
