"""Multi-unit symbol support (RW-S1, spec-multi-unit tests 1-15b + 17).

Covers C1 (add_component/add_components_bulk unit placement + guards),
C2 (unit-aware pin resolution incl. the single-unit fold rule and the C2b
parity snapshot), C3 (connect_pins per-unit semantics + common-pin
materializations), C3b (_pin_obstacles), and C4 (reads, annotate, validate,
move/remove). Test 16 (get_symbol_info units, C5) lives in
tests/test_tools_library.py; the test numbering below follows the spec.

Fixtures are self-contained: every schematic embeds its lib_symbols cache so
no system KiCad library is needed. The checked-in
tests/fixtures/schematics/multiunit.kicad_sch holds the canonical two-unit
placement (U1 unit 1 at (100, 50), unit 2 at (100, 80)) used by the
pin-resolution read tests. Test 17 is the kicad-cli oracle round-trip and
skips when kicad-cli is absent.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.utils.platform_helper import find_kicad_cli
from kicad_mcp.utils.sexp_parser import parse_sexp_content, parse_sexp_file

FIXTURES = Path(__file__).parent / "fixtures"
MULTIUNIT_FIXTURE = FIXTURES / "schematics" / "multiunit.kicad_sch"
PARITY_SNAPSHOT = FIXTURES / "rotation_audit" / "resolver_parity_snapshot.json"

SCH_UUID = "11111111-1111-1111-1111-111111111111"

SCH_HEADER = f"""\
(kicad_sch
  (version 20250114)
  (generator "kicad_mcp")
  (generator_version "9.0")
  (uuid "{SCH_UUID}")

  (paper "A4")

"""

SCH_FOOTER = """\
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Two-unit symbol: unit 0 carries the common pin 5 (COM), unit 1 carries
# pins 1/2, unit 2 carries pins 3/4 — same shape as the checked-in fixture.
DUAL_LIB_SYMBOL = """\
    (symbol "Test:DUAL" (pin_numbers (hide no)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 0 0)
        (effects (font (size 1.27 1.27)))
      )
      (property "Value" "DUAL" (at 0 2 0)
        (effects (font (size 1.27 1.27)))
      )
      (symbol "DUAL_0_1"
        (pin passive line (at 0 7.62 270) (length 2.54)
          (name "COM" (effects (font (size 1.27 1.27))))
          (number "5" (effects (font (size 1.27 1.27))))
        )
      )
      (symbol "DUAL_1_1"
        (pin passive line (at -7.62 0 0) (length 2.54)
          (name "A1" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 7.62 0 180) (length 2.54)
          (name "B1" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
      (symbol "DUAL_2_1"
        (pin passive line (at -7.62 0 0) (length 2.54)
          (name "A2" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 7.62 0 180) (length 2.54)
          (name "B2" (effects (font (size 1.27 1.27))))
          (number "4" (effects (font (size 1.27 1.27))))
        )
      )
    )
"""

# Single-unit symbol authored the stock way: every pin in "<NAME>_0_1"
# (the C2 fold-rule case — unit-0 pins belong to unit 1, not "common").
TEST_R_LIB_SYMBOL = """\
    (symbol "Test:R" (pin_numbers (hide no)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (symbol "R_0_1"
        (pin passive line (at -2.54 0 0)
          (length 0)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 2.54 0 180)
          (length 0)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
    )
"""

# Single-unit symbol with its pins in the "_1_1" section instead.
TEST_RS_LIB_SYMBOL = """\
    (symbol "Test:RS" (pin_numbers (hide no)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (symbol "RS_1_1"
        (pin passive line (at -2.54 0 0)
          (length 0)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 2.54 0 180)
          (length 0)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
    )
"""


def _instance(
    lib_id: str, ref: str, x: float, y: float, unit: int, uid: str,
    value: str = "V",
) -> str:
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit {unit})\n'
        f'    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
        f'    (uuid "{uid}")\n'
        f'    (property "Reference" "{ref}" (at {x} {y - 2} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Value" "{value}" (at {x} {y + 2} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (instances\n'
        f'      (project ""\n'
        f'        (path "/{SCH_UUID}"\n'
        f'          (reference "{ref}") (unit {unit})\n'
        f'        )\n'
        f'      )\n'
        f'    )\n'
        f'  )\n'
    )


def _make_schematic(
    tmp_path: Path, lib_symbols: str, instances: str = "",
    name: str = "test.kicad_sch",
) -> Path:
    sch = (
        SCH_HEADER
        + "  (lib_symbols\n"
        + lib_symbols
        + "  )\n\n"
        + instances
        + SCH_FOOTER
    )
    f = tmp_path / name
    f.write_text(sch, encoding="utf-8")
    return f


@pytest.fixture
def ops() -> FileSchematicOps:
    return FileSchematicOps()


@pytest.fixture
def sch_dual_unit1(tmp_path: Path) -> Path:
    """Test:DUAL with only unit 1 placed as U1 at (100, 50)."""
    return _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL,
        _instance("Test:DUAL", "U1", 100, 50, 1,
                  "22222222-2222-2222-2222-222222222221", "DUAL"),
    )


# Grid-aligned two-unit placement (all pins land on the 1.27 mm grid) so
# connect_pins output positions are exact — unit 1 at (101.6, 50.8),
# unit 2 at (101.6, 81.28).
@pytest.fixture
def sch_dual_grid(tmp_path: Path) -> Path:
    return _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL,
        _instance("Test:DUAL", "U1", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222221", "DUAL")
        + _instance("Test:DUAL", "U1", 101.6, 81.28, 2,
                    "22222222-2222-2222-2222-222222222222", "DUAL"),
    )


@pytest.fixture
def sch_dual_grid_unit1(tmp_path: Path) -> Path:
    return _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL,
        _instance("Test:DUAL", "U1", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222221", "DUAL"),
    )


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    """The checked-in two-unit fixture, copied so tests can mutate it."""
    dest = tmp_path / "multiunit.kicad_sch"
    dest.write_text(MULTIUNIT_FIXTURE.read_text(encoding="utf-8"),
                    encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Test 1 — placing a second unit creates a sibling block
# ---------------------------------------------------------------------------

def test_add_second_unit_creates_sibling_block(
    ops: FileSchematicOps, sch_dual_unit1: Path,
) -> None:
    result = ops.add_component(
        sch_dual_unit1, "Test:DUAL", "U1", "DUAL", 114.3, 63.5, unit=2,
    )
    assert result["unit"] == 2
    assert result["reference"] == "U1"

    content = sch_dual_unit1.read_text(encoding="utf-8")
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U1")
    assert len(blocks) == 2
    assert sorted(b["unit"] for b in blocks) == [1, 2]
    assert all(b["lib_id"] == "Test:DUAL" for b in blocks)

    # Distinct uuids per block.
    uuids = []
    for b in blocks:
        m = re.search(r'\(uuid "([^"]+)"\)', content[b["start"]:b["end"] + 1])
        assert m is not None
        uuids.append(m.group(1))
    assert len(set(uuids)) == 2

    # (unit 2) present in both the block header and the instances path.
    assert '(reference "U1") (unit 2)' in content
    unit2_block = next(b for b in blocks if b["unit"] == 2)
    assert re.search(r"\(unit\s+2\)", content[unit2_block["start"]:unit2_block["start"] + 120])


# ---------------------------------------------------------------------------
# Test 2 — out-of-range unit refuses with the exact error string
# ---------------------------------------------------------------------------

def test_add_unit_out_of_range_refuses(
    ops: FileSchematicOps, sch_dual_unit1: Path, tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=re.escape(
            "symbol 'Test:DUAL' has 2 unit(s); requested unit 3")):
        ops.add_component(sch_dual_unit1, "Test:DUAL", "U2", "DUAL",
                          50, 50, unit=3)

    # Single-unit symbol: any unit other than 1 is out of range.
    sch = _make_schematic(tmp_path, TEST_R_LIB_SYMBOL, name="single.kicad_sch")
    with pytest.raises(ValueError, match=re.escape(
            "symbol 'Test:R' has 1 unit(s); requested unit 5")):
        ops.add_component(sch, "Test:R", "R1", "10k", 50, 50, unit=5)


# ---------------------------------------------------------------------------
# Test 3 — duplicate (reference, unit) refuses; a different unit is allowed
# ---------------------------------------------------------------------------

def test_add_duplicate_ref_unit_refuses(
    ops: FileSchematicOps, sch_dual_unit1: Path,
) -> None:
    with pytest.raises(ValueError, match=re.escape(
            "reference 'U1' unit 1 is already placed")):
        ops.add_component(sch_dual_unit1, "Test:DUAL", "U1", "DUAL",
                          130, 50, unit=1)


def test_add_same_ref_different_unit_allowed(
    ops: FileSchematicOps, sch_dual_unit1: Path,
) -> None:
    result = ops.add_component(
        sch_dual_unit1, "Test:DUAL", "U1", "DUAL", 130, 50, unit=2,
    )
    assert result["unit"] == 2
    content = sch_dual_unit1.read_text(encoding="utf-8")
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U1")
    assert sorted(b["unit"] for b in blocks) == [1, 2]


# ---------------------------------------------------------------------------
# Test 4 — one reference cannot span two different symbols
# ---------------------------------------------------------------------------

def test_add_unit_lib_mismatch_refuses(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL + TEST_R_LIB_SYMBOL,
        _instance("Test:DUAL", "U1", 100, 50, 1,
                  "22222222-2222-2222-2222-222222222221", "DUAL"),
    )
    with pytest.raises(ValueError, match=re.escape(
            "reference 'U1' is already placed as 'Test:DUAL'; "
            "all units must share one symbol")):
        ops.add_component(sch, "Test:R", "U1", "10k", 130, 50)


# ---------------------------------------------------------------------------
# Test 4b — bulk path: (reference, unit)-keyed batch guard
# ---------------------------------------------------------------------------

def test_bulk_two_units_one_ref_one_batch_allowed(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(tmp_path, DUAL_LIB_SYMBOL)
    result = ops.add_components_bulk(sch, [
        {"lib_id": "Test:DUAL", "reference": "U9", "value": "DUAL",
         "x": 101.6, "y": 50.8, "unit": 1},
        {"lib_id": "Test:DUAL", "reference": "U9", "value": "DUAL",
         "x": 101.6, "y": 81.28, "unit": 2},
    ])
    assert result.get("status") != "refused"
    assert len(result["placed"]) == 2
    assert result["failed"] == []
    assert sorted(p["unit"] for p in result["placed"]) == [1, 2]

    content = sch.read_text(encoding="utf-8")
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U9")
    assert sorted(b["unit"] for b in blocks) == [1, 2]


def test_bulk_duplicate_ref_unit_pair_refused(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(tmp_path, DUAL_LIB_SYMBOL)
    before = sch.read_text(encoding="utf-8")
    result = ops.add_components_bulk(sch, [
        {"lib_id": "Test:DUAL", "reference": "U9", "value": "DUAL",
         "x": 101.6, "y": 50.8, "unit": 1},
        {"lib_id": "Test:DUAL", "reference": "U9", "value": "DUAL",
         "x": 101.6, "y": 81.28, "unit": 1},
    ])
    assert result["status"] == "refused"
    assert "duplicate (reference, unit) within the batch" in result["reason"]
    assert "U9 (unit 1)" in result["reason"]
    assert result["placed"] == []
    assert sch.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Test 5 — pins resolve against their OWNING unit's placement
# ---------------------------------------------------------------------------

def test_pin_positions_per_unit(ops: FileSchematicOps) -> None:
    info = ops.get_symbol_pin_positions(MULTIUNIT_FIXTURE, "U1")
    assert "error" not in info
    assert info["units_placed"] == [1, 2]
    # Pin 3 belongs to unit 2 at (100, 80) — the pre-fix resolver conflated
    # every pin onto the first block and reported (92.38, 50).
    assert info["pin_positions"]["3"] == {"x": 92.38, "y": 80.0, "unit": 2}
    assert info["pin_positions"]["1"] == {"x": 92.38, "y": 50.0, "unit": 1}
    assert info["unit_positions"]["1"]["y"] == 50.0
    assert info["unit_positions"]["2"]["y"] == 80.0


# ---------------------------------------------------------------------------
# Test 6 — a common (unit-0) pin lists every placed materialization
# ---------------------------------------------------------------------------

def test_pin_positions_common_pin_lists_all_instances(
    ops: FileSchematicOps,
) -> None:
    info = ops.get_symbol_pin_positions(MULTIUNIT_FIXTURE, "U1")
    assert info["common_pin_positions"]["5"] == [
        {"unit": 1, "x": 100.0, "y": 42.38},
        {"unit": 2, "x": 100.0, "y": 72.38},
    ]
    assert "5" not in info["pin_positions"]


# ---------------------------------------------------------------------------
# Test 7 — a pin of an unplaced unit does not exist on the sheet
# ---------------------------------------------------------------------------

def test_pin_positions_unplaced_unit_pin_absent(
    ops: FileSchematicOps, sch_dual_unit1: Path,
) -> None:
    info = ops.get_symbol_pin_positions(sch_dual_unit1, "U1")
    assert info["units_placed"] == [1]
    assert set(info["pin_positions"]) == {"1", "2"}
    assert "3" not in info["pin_positions"]
    assert "3" not in info["common_pin_positions"]
    assert info["unplaced_pins"] == {"3": 2, "4": 2}


# ---------------------------------------------------------------------------
# Test 8 — single-unit payload stays backward compatible
# ---------------------------------------------------------------------------

def test_single_unit_payload_backward_compatible(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(
        tmp_path, TEST_RS_LIB_SYMBOL,
        _instance("Test:RS", "R5", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222225", "10k"),
    )
    info = ops.get_symbol_pin_positions(sch, "R5")
    # Today's keys all present…
    for key in ("reference", "lib_id", "position", "rotation", "mirror",
                "pin_positions"):
        assert key in info
    # …plus the multi-unit additions.
    assert info["units_placed"] == [1]
    assert set(info["unit_positions"]) == {"1"}
    assert info["position"] == {"x": 101.6, "y": 50.8}
    assert info["pin_positions"]["1"] == {"x": 99.06, "y": 50.8, "unit": 1}
    assert info["common_pin_positions"] == {}
    assert "unplaced_pins" not in info


# ---------------------------------------------------------------------------
# Test 8b — the single-unit fold rule: "_0_1" pins belong to unit 1
# ---------------------------------------------------------------------------

def test_single_unit_zero_section_pins_stay_in_pin_positions(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(
        tmp_path, TEST_R_LIB_SYMBOL,
        _instance("Test:R", "R1", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222226", "10k"),
    )
    info = ops.get_symbol_pin_positions(sch, "R1")
    # Stock symbols keep every pin in "<NAME>_0_1"; with one unit those pins
    # are NOT common — they fold into unit 1 (C2).
    assert set(info["pin_positions"]) == {"1", "2"}
    assert info["pin_positions"]["1"]["unit"] == 1
    assert info["common_pin_positions"] == {}


# ---------------------------------------------------------------------------
# Test 8c — C2b parity: the rewritten resolver reproduces the pre-rewrite
# snapshot exactly on every single-unit fixture
# ---------------------------------------------------------------------------

def test_resolver_single_unit_parity_snapshot(ops: FileSchematicOps) -> None:
    snapshot = json.loads(PARITY_SNAPSHOT.read_text(encoding="utf-8"))
    assert snapshot, "parity snapshot is empty"

    for rel, expected in snapshot.items():
        sch = FIXTURES / rel
        assert sch.is_file(), f"snapshot fixture missing: {rel}"
        if "__parse_error__" in expected:
            continue
        tree = parse_sexp_file(sch)

        # Same reference-discovery scan the snapshot harness used.
        refs: list[str] = []
        for node in tree:
            if not (isinstance(node, list) and len(node) >= 2
                    and node[0] == "symbol"):
                continue
            for child in node[1:]:
                if (isinstance(child, list) and len(child) >= 3
                        and child[0] == "property"
                        and child[1] == "Reference"):
                    if child[2] not in refs:
                        refs.append(child[2])
                    break

        rebuilt: dict = {}
        for ref in refs:
            info = ops._resolve_pin_positions(tree, ref, sch)
            if "error" in info:
                rebuilt[ref] = {"error": info["error"]}
            else:
                rebuilt[ref] = {
                    "pin_positions": {
                        num: {"x": pos["x"], "y": pos["y"]}
                        for num, pos in info.get("pin_positions", {}).items()
                    },
                }
        assert rebuilt == expected, f"resolver parity broken for {rel}"


# ---------------------------------------------------------------------------
# Test 9 — connect_pins lands the label at the owning unit's location
# ---------------------------------------------------------------------------

def test_connect_pins_targets_owning_unit(
    ops: FileSchematicOps, sch_dual_grid: Path,
) -> None:
    result = ops.connect_pins_bulk(sch_dual_grid, ["U1.4"], "NETB",
                                   stub_length=0)
    assert result["failed"] == []
    entry = result["connected"][0]
    # Pin 4 belongs to unit 2 at (101.6, 81.28) — its absolute position is
    # (109.22, 81.28), on unit 2's row, not unit 1's.
    assert entry["x"] == 109.22
    assert entry["y"] == 81.28
    assert entry["instances"] == 1
    assert "placements" not in entry

    content = sch_dual_grid.read_text(encoding="utf-8")
    assert '(label "NETB" (at 109.22 81.28 0)' in content


# ---------------------------------------------------------------------------
# Test 9b — the stub points away from the OWNING unit's body
# ---------------------------------------------------------------------------

def test_connect_pins_stub_direction_from_owning_unit(
    ops: FileSchematicOps, sch_dual_grid: Path,
) -> None:
    result = ops.connect_pins_bulk(sch_dual_grid, ["U1.3"], "NETC")
    assert result["failed"] == []
    entry = result["connected"][0]
    # Pin 3 sits at (93.98, 81.28), left of unit 2's body at (101.6, 81.28):
    # the stub extends outward along -x to (91.44, 81.28). Directed from
    # unit 1's body instead, the dominant direction would flip to +y.
    assert (entry["x"], entry["y"]) == (91.44, 81.28)

    content = sch_dual_grid.read_text(encoding="utf-8")
    assert "(xy 93.98 81.28) (xy 91.44 81.28)" in content


# ---------------------------------------------------------------------------
# Test 10 — a common pin nets EVERY placed materialization
# ---------------------------------------------------------------------------

def test_connect_pins_common_pin_labels_every_instance(
    ops: FileSchematicOps, sch_dual_grid: Path,
) -> None:
    result = ops.connect_pins_bulk(sch_dual_grid, ["U1.5"], "COMX",
                                   stub_length=0)
    assert result["failed"] == []
    entry = result["connected"][0]
    assert entry["instances"] == 2

    placements = entry["placements"]
    assert [
        {k: p[k] for k in ("unit", "x", "y")} for p in placements
    ] == [
        {"unit": 1, "x": 101.6, "y": 43.18},
        {"unit": 2, "x": 101.6, "y": 73.66},
    ]
    for p in placements:
        assert set(p) == {"unit", "x", "y", "label_uuid", "wire_uuid"}
        assert p["wire_uuid"] is None  # stub_length=0 → label on the pin
    assert len({p["label_uuid"] for p in placements}) == 2

    # Top-level fields mirror the lowest-unit placement.
    assert entry["x"] == placements[0]["x"]
    assert entry["y"] == placements[0]["y"]
    assert entry["label_uuid"] == placements[0]["label_uuid"]
    assert entry["wire_uuid"] is None

    content = sch_dual_grid.read_text(encoding="utf-8")
    assert content.count('(label "COMX"') == 2


# ---------------------------------------------------------------------------
# Test 10b — the obstacle cloud covers every materialization, once per ref
# (per amendment A3 the cloud is pin points only — no body rectangles; the
# "per-unit bodies" contract maps to per-unit pin coordinates + per-unit
# stub origins)
# ---------------------------------------------------------------------------

def test_pin_obstacles_include_common_materializations(
    ops: FileSchematicOps,
) -> None:
    tree = parse_sexp_file(MULTIUNIT_FIXTURE)
    obstacles = ops._pin_obstacles(tree, MULTIUNIT_FIXTURE, {})
    ids = [o["pin_id"] for o in obstacles]
    assert len(obstacles) == 6
    assert set(ids) == {"U1.1", "U1.2", "U1.3", "U1.4", "U1.5"}
    # The common pin materializes at both placed units, same pin_id.
    assert ids.count("U1.5") == 2
    assert {o["y"] for o in obstacles if o["pin_id"] == "U1.5"} == {42.38, 72.38}
    # A multi-unit ref spans two blocks but is processed once — its unit-1
    # pins must not enter the cloud twice.
    assert ids.count("U1.1") == 1


def test_pin_obstacles_per_unit_bodies(ops: FileSchematicOps) -> None:
    tree = parse_sexp_file(MULTIUNIT_FIXTURE)
    obstacles = ops._pin_obstacles(tree, MULTIUNIT_FIXTURE, {})
    # Unit-2 pins enter the cloud at unit 2's placement (y=80), not folded
    # onto unit 1's body (y=50).
    pin3 = next(o for o in obstacles if o["pin_id"] == "U1.3")
    assert (pin3["x"], pin3["y"]) == (92.38, 80.0)
    pin1 = next(o for o in obstacles if o["pin_id"] == "U1.1")
    assert (pin1["x"], pin1["y"]) == (92.38, 50.0)


# ---------------------------------------------------------------------------
# Test 11 — connecting a pin of an unplaced unit fails that item only
# ---------------------------------------------------------------------------

def test_connect_pins_unplaced_unit_fails_item(
    ops: FileSchematicOps, sch_dual_grid_unit1: Path,
) -> None:
    result = ops.connect_pins_bulk(sch_dual_grid_unit1, ["U1.3", "U1.1"], "N1")
    assert len(result["failed"]) == 1
    assert result["failed"][0]["pin"] == "U1.3"
    assert result["failed"][0]["reason"] == (
        "pin '3' of 'U1' belongs to unit 2, which is not placed"
    )
    assert len(result["connected"]) == 1
    assert result["connected"][0]["pin"] == "U1.1"
    assert sch_dual_grid_unit1.read_text(encoding="utf-8").count('(label "N1"') == 1


# ---------------------------------------------------------------------------
# Test 12 — readers report one row per reference with units_placed
# ---------------------------------------------------------------------------

def test_get_symbols_one_row_with_units_placed(ops: FileSchematicOps) -> None:
    symbols = ops.get_symbols(MULTIUNIT_FIXTURE)
    rows = [s for s in symbols if s.get("reference") == "U1"]
    assert len(rows) == 1
    row = rows[0]
    assert row["units_placed"] == [1, 2]
    insts = sorted(row["unit_instances"], key=lambda i: i["unit"])
    assert [i["unit"] for i in insts] == [1, 2]
    assert insts[0]["position"]["y"] == 50.0
    assert insts[1]["position"]["y"] == 80.0
    assert "duplicate_units" not in row
    assert "lib_id_conflict" not in row


# ---------------------------------------------------------------------------
# Test 13 — annotate renames all unit blocks of one component together
# ---------------------------------------------------------------------------

def test_annotate_renames_all_unit_blocks(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    sch = _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL + TEST_R_LIB_SYMBOL,
        _instance("Test:DUAL", "U?", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222231", "DUAL")
        + _instance("Test:DUAL", "U?", 101.6, 81.28, 2,
                    "22222222-2222-2222-2222-222222222232", "DUAL")
        + _instance("Test:R", "R?", 50.8, 50.8, 1,
                    "22222222-2222-2222-2222-222222222233", "10k")
        + _instance("Test:R", "R?", 50.8, 63.5, 1,
                    "22222222-2222-2222-2222-222222222234", "10k"),
    )
    result = ops.annotate(sch)
    assert result["annotated_count"] == 4

    content = sch.read_text(encoding="utf-8")
    # Two distinct units of one unannotated reference = ONE component: both
    # blocks (property AND instances entry) become U1.
    u1_blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U1")
    assert sorted(b["unit"] for b in u1_blocks) == [1, 2]
    assert '(reference "U1") (unit 2)' in content
    assert "U?" not in content
    # A repeated (reference, unit) = separate components: R1 and R2.
    assert len(FileSchematicOps._find_symbol_blocks_by_reference(content, "R1")) == 1
    assert len(FileSchematicOps._find_symbol_blocks_by_reference(content, "R2")) == 1


# ---------------------------------------------------------------------------
# Test 14 — validation: two units of one ref is NOT a duplicate; the same
# unit twice IS
# ---------------------------------------------------------------------------

def test_validate_schematic_same_ref_two_units_not_duplicate(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    result = ops.validate_schematic(MULTIUNIT_FIXTURE)
    dups = [v for v in result.get("violations", [])
            if v.get("type") == "duplicate_reference"]
    assert dups == []

    # Same (reference, unit) twice — a real duplicate.
    sch = _make_schematic(
        tmp_path, DUAL_LIB_SYMBOL,
        _instance("Test:DUAL", "U2", 101.6, 50.8, 1,
                  "22222222-2222-2222-2222-222222222241", "DUAL")
        + _instance("Test:DUAL", "U2", 152.4, 50.8, 1,
                    "22222222-2222-2222-2222-222222222242", "DUAL"),
    )
    result = ops.validate_schematic(sch)
    dups = [v for v in result.get("violations", [])
            if v.get("type") == "duplicate_reference"]
    assert len(dups) == 1
    assert dups[0]["description"] == (
        "Duplicate reference designator 'U2' (unit 1 placed more than once)"
    )


# ---------------------------------------------------------------------------
# Test 15 — remove_component: unit-scoped and whole-component semantics
# ---------------------------------------------------------------------------

def test_remove_component_unit_scoped(
    ops: FileSchematicOps, fixture_copy: Path,
) -> None:
    with pytest.raises(ValueError, match=re.escape(
            "reference 'U1' unit 3 is not placed")):
        ops.remove_component(fixture_copy, "U1", unit=3)

    result = ops.remove_component(fixture_copy, "U1", unit=2)
    assert result == {"reference": "U1", "removed": True, "unit": 2}

    content = fixture_copy.read_text(encoding="utf-8")
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U1")
    assert [b["unit"] for b in blocks] == [1]


def test_remove_component_all_units_default(
    ops: FileSchematicOps, fixture_copy: Path,
) -> None:
    result = ops.remove_component(fixture_copy, "U1")
    assert result == {
        "reference": "U1", "removed": True, "units_removed": [1, 2],
    }
    content = fixture_copy.read_text(encoding="utf-8")
    assert FileSchematicOps._find_symbol_blocks_by_reference(content, "U1") == []


# ---------------------------------------------------------------------------
# Test 15b — move_component moves only the requested unit's block
# ---------------------------------------------------------------------------

def test_move_component_unit_scoped(
    ops: FileSchematicOps, fixture_copy: Path,
) -> None:
    result = ops.move_component(fixture_copy, "U1", 120.65, 90.17, unit=2)
    assert result["unit"] == 2
    assert result["position"] == {"x": 120.65, "y": 90.17}

    info = ops.get_symbol_pin_positions(fixture_copy, "U1")
    assert info["unit_positions"]["1"] == {"x": 100.0, "y": 50.0, "rotation": 0.0}
    assert info["unit_positions"]["2"] == {"x": 120.65, "y": 90.17, "rotation": 0.0}


# ---------------------------------------------------------------------------
# Test 17 — kicad-cli oracle round-trip (skips when kicad-cli is absent).
# Build the two-unit schematic through the tools, then let KiCad's own
# engines confirm: the netlist merges to ONE component, the intended net
# spans pins of both units, and netting the common pin at every
# materialization (C3) produces zero different_unit_net ERC violations.
# Assertions are version-invariant (KiCad 9 and 10).
# ---------------------------------------------------------------------------

def _erc_counts(cli: Path, sch: Path, out: Path) -> dict[str, int]:
    subprocess.run(
        [str(cli), "sch", "erc", "--format", "json", "--units", "mm",
         "--severity-all", "-o", str(out), str(sch)],
        capture_output=True, text=True, timeout=120,
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            counts[v["type"]] = counts.get(v["type"], 0) + 1
    return counts


def _children(node: list, tag: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def test_multiunit_netlist_oracle(
    ops: FileSchematicOps, tmp_path: Path,
) -> None:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")

    sch = _make_schematic(tmp_path, DUAL_LIB_SYMBOL)
    ops.add_component(sch, "Test:DUAL", "U1", "DUAL", 101.6, 50.8, unit=1)
    ops.add_component(sch, "Test:DUAL", "U1", "DUAL", 101.6, 81.28, unit=2)

    # One net spanning both units (unit 1's pin 2 + unit 2's pin 3), the
    # common pin netted at BOTH materializations via C3, and no-connects on
    # the leftovers.
    r = ops.connect_pins_bulk(sch, ["U1.2", "U1.3"], "SPAN")
    assert r["failed"] == []
    r = ops.connect_pins_bulk(sch, ["U1.5"], "COMBOTH")
    assert r["failed"] == []
    assert r["connected"][0]["instances"] == 2
    info = ops.get_symbol_pin_positions(sch, "U1")
    for pin in ("1", "4"):
        pos = info["pin_positions"][pin]
        ops.add_no_connect(sch, pos["x"], pos["y"])

    # Netlist: exactly one component for U1, and SPAN carries both units'
    # pins on the one merged component.
    net_out = tmp_path / "oracle.net"
    proc = subprocess.run(
        [str(cli), "sch", "export", "netlist",
         "--output", str(net_out), "--format", "kicadsexpr", str(sch)],
        capture_output=True, text=True, timeout=120,
    )
    assert net_out.is_file(), f"netlist export failed: {proc.stderr}"
    netlist = parse_sexp_content(net_out.read_text(encoding="utf-8"))

    comps = _children(_children(netlist, "components")[0], "comp")
    u1_comps = [c for c in comps
                if any(ch[1] == "U1" for ch in _children(c, "ref"))]
    assert len(u1_comps) == 1, "multi-unit blocks must merge to one component"

    span_nodes: set[tuple[str, str]] = set()
    for net in _children(_children(netlist, "nets")[0], "net"):
        name = next((str(ch[1]) for ch in _children(net, "name")), "")
        if name.split("/")[-1] != "SPAN":
            continue
        for node in _children(net, "node"):
            ref = next((str(ch[1]) for ch in _children(node, "ref")), "")
            pin = next((str(ch[1]) for ch in _children(node, "pin")), "")
            span_nodes.add((ref, pin))
    assert {("U1", "2"), ("U1", "3")} <= span_nodes, (
        f"SPAN must span both units' pins, got {span_nodes}"
    )

    # ERC: connecting the common pin at every materialization leaves zero
    # different_unit_net violations.
    counts = _erc_counts(cli, sch, tmp_path / "erc.json")
    assert counts.get("different_unit_net", 0) == 0, counts
