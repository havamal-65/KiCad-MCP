"""Unit tests for the symbol/footprint pair validator (§6.2, REQ-TEST-001/002).

The validator (`run_validate_symbol_footprint_pairs`) loads each symbol's
Footprint .kicad_mod and checks the symbol's pin numbers are a subset of the
footprint's pad numbers. Handcrafted fixtures under tests/fixtures/ exercise the
happy path plus each failure mode; footprint + symbol library resolution is
patched to point at those fixtures (pattern from test_validate_connector_orientations).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.drc import run_validate_symbol_footprint_pairs

_SCH_DIR = Path(__file__).parent / "fixtures" / "schematics"
_FP_DIR = Path(__file__).parent / "fixtures" / "footprints"
_SYM_DIR = Path(__file__).parent / "fixtures" / "symbols"


@pytest.fixture(autouse=True)
def _fixture_libraries(monkeypatch):
    """Resolve footprint + symbol lib_ids against the fixture dirs only."""
    # _load_kicad_mod: step 1 (fp-lib-table map) misses, step 2 scans these dirs.
    monkeypatch.setattr(
        "kicad_mcp.utils.fp_lib_table.get_footprint_library_map",
        lambda project_dir=None: {},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_paths.find_footprint_libraries",
        lambda project_dir=None: [_FP_DIR / "TestPkg"],
    )
    # FileLibraryOps caches find_symbol_libraries() at construction.
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_paths.find_symbol_libraries",
        lambda: sorted(_SYM_DIR.glob("*.kicad_sym")),
    )


# ── REQ-TEST-001 — happy path ────────────────────────────────────────────────

def test_clean_schematic_passes():
    result = run_validate_symbol_footprint_pairs(_SCH_DIR / "clean.kicad_sch")
    assert result["passed"] is True, result
    assert result["checked"] == 2, result
    assert result["mismatches"] == []
    assert result["unresolvable"] == []
    assert result["warnings"] == []
    assert result["over_limit"] is False


# ── REQ-TEST-002 — four failure modes ────────────────────────────────────────

def test_pin_not_in_footprint_blocks():
    result = run_validate_symbol_footprint_pairs(_SCH_DIR / "pin_missing.kicad_sch")
    assert result["passed"] is False, result
    assert len(result["mismatches"]) == 1, result
    mm = result["mismatches"][0]
    assert mm["ref"] == "U1"
    assert mm["footprint"] == "TestPkg:R_4pad"
    assert mm["missing"] == ["5"]
    assert mm["symbol_pins"] == ["1", "2", "3", "4", "5"]
    assert mm["footprint_pads"] == ["1", "2", "3", "4"]


def test_symbol_lib_unresolvable_blocks():
    result = run_validate_symbol_footprint_pairs(_SCH_DIR / "symbol_lib_missing.kicad_sch")
    assert result["passed"] is False, result
    assert len(result["unresolvable"]) == 1, result
    un = result["unresolvable"][0]
    assert un["ref"] == "U1"
    assert un["reason"] == "symbol library not found"


def test_footprint_lib_unresolvable_blocks():
    result = run_validate_symbol_footprint_pairs(_SCH_DIR / "footprint_lib_missing.kicad_sch")
    assert result["passed"] is False, result
    assert len(result["unresolvable"]) == 1, result
    un = result["unresolvable"][0]
    assert un["ref"] == "R1"
    assert un["footprint"] == "NoSuchLib:Foo"
    assert un["reason"] == "library not found"


def test_extra_pads_warn_only():
    result = run_validate_symbol_footprint_pairs(_SCH_DIR / "extra_pads.kicad_sch")
    assert result["passed"] is True, result          # extras don't block
    assert result["mismatches"] == []
    assert len(result["warnings"]) == 1, result
    w = result["warnings"][0]
    assert w["ref"] == "U1"
    assert w["extra_pads"] == ["TP"]
