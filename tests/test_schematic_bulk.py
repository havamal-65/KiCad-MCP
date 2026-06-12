"""Tests for the bulk schematic-capture tools.

Covers FileSchematicOps.add_components_bulk, add_power_symbols_bulk, and
connect_pins_bulk. The bulk methods must perform a single file read +
single file write regardless of input size.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps


# ---------------------------------------------------------------------------
# Fixtures — self-contained schematics with embedded lib_symbols entries so
# tests don't depend on a real KiCad symbol library being installed.
# ---------------------------------------------------------------------------

SCH_HEADER = """\
(kicad_sch
  (version 20250114)
  (generator "kicad_mcp")
  (generator_version "9.0")
  (uuid "11111111-1111-1111-1111-111111111111")

  (paper "A4")

"""

SCH_FOOTER = """\
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Bare symbol entry for a generic Device:R — pin definitions omitted because
# the bulk-component tests don't need pin resolution. The presence of the
# entry in lib_symbols is enough to short-circuit _ensure_lib_symbol_cached.
DEVICE_R_LIB_SYMBOL = """\
    (symbol "Device:R" (pin_numbers (hide no)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
    )
"""

# Power symbol stub — same trick: present in lib_symbols, pins not needed.
POWER_VCC_LIB_SYMBOL = """\
    (symbol "power:VCC" (power)
      (exclude_from_sim no) (in_bom yes) (on_board yes)
    )
"""

POWER_GND_LIB_SYMBOL = """\
    (symbol "power:GND" (power)
      (exclude_from_sim no) (in_bom yes) (on_board yes)
    )
"""

# Test:R with explicit pin definitions so connect_pins tests can resolve
# absolute pin coordinates without needing a real Device.kicad_sym library.
# Pin 1 at lib coord (-2.54, 0); pin 2 at (2.54, 0). Library is Y-up; the
# resolver negates Y to get schematic Y-down.
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

R1_INSTANCE = """\
  (symbol (lib_id "Test:R") (at 100 50 0) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "R1" (at 100 48 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "10k" (at 100 52 0)
      (effects (font (size 1.27 1.27)))
    )
    (instances
      (project ""
        (path "/11111111-1111-1111-1111-111111111111"
          (reference "R1") (unit 1)
        )
      )
    )
  )
"""

# A power symbol instance with #PWR007 already taken — used to test that
# the bulk method resumes from the current max.
EXISTING_PWR007_INSTANCE = """\
  (symbol (lib_id "power:VCC") (at 50 30 0) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "33333333-3333-3333-3333-333333333333")
    (property "Reference" "#PWR007" (at 50 28 0)
      (effects (font (size 1.27 1.27)) (hide yes))
    )
    (property "Value" "VCC" (at 50 32 0)
      (effects (font (size 1.27 1.27)))
    )
    (instances
      (project ""
        (path "/11111111-1111-1111-1111-111111111111"
          (reference "#PWR007") (unit 1)
        )
      )
    )
  )
"""


def _make_schematic(tmp_path: Path, lib_symbols: str, instances: str = "") -> Path:
    sch = (
        SCH_HEADER
        + "  (lib_symbols\n"
        + lib_symbols
        + "  )\n\n"
        + instances
        + SCH_FOOTER
    )
    f = tmp_path / "test.kicad_sch"
    f.write_text(sch, encoding="utf-8")
    return f


@pytest.fixture
def sch_with_device_r(tmp_path: Path) -> Path:
    return _make_schematic(tmp_path, DEVICE_R_LIB_SYMBOL)


@pytest.fixture
def sch_with_power_libs(tmp_path: Path) -> Path:
    return _make_schematic(tmp_path, POWER_VCC_LIB_SYMBOL + POWER_GND_LIB_SYMBOL)


@pytest.fixture
def sch_with_existing_pwr(tmp_path: Path) -> Path:
    return _make_schematic(
        tmp_path, POWER_VCC_LIB_SYMBOL, instances=EXISTING_PWR007_INSTANCE,
    )


def _resistor_instance(ref: str, x: float, y: float, value: str = "10k") -> str:
    """Build a placed Test:R instance with property labels at the canonical
    offsets used by FileSchematicOps.add_component (label y = symbol y ± 2)."""
    sym_uuid = f"{hash(ref) & 0xFFFFFFFF:08x}-1234-5678-9abc-{hash(ref) & 0xFFFFFFFFFFFF:012x}"
    return (
        f'  (symbol (lib_id "Test:R") (at {x} {y} 0) (unit 1)\n'
        f'    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
        f'    (uuid "{sym_uuid}")\n'
        f'    (property "Reference" "{ref}" (at {x} {y - 2} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Value" "{value}" (at {x} {y + 2} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (instances\n'
        f'      (project ""\n'
        f'        (path "/11111111-1111-1111-1111-111111111111"\n'
        f'          (reference "{ref}") (unit 1)\n'
        f'        )\n'
        f'      )\n'
        f'    )\n'
        f'  )\n'
    )


@pytest.fixture
def sch_with_three_resistors(tmp_path: Path) -> Path:
    instances = (
        _resistor_instance("R1", 100.0, 50.0, "10k")
        + _resistor_instance("R2", 110.0, 50.0, "5k")
        + _resistor_instance("R3", 120.0, 50.0, "1k")
    )
    return _make_schematic(tmp_path, TEST_R_LIB_SYMBOL, instances=instances)


@pytest.fixture
def sch_with_r1(tmp_path: Path) -> Path:
    return _make_schematic(tmp_path, TEST_R_LIB_SYMBOL, instances=R1_INSTANCE)


# ---------------------------------------------------------------------------
# IO call counting helper
# ---------------------------------------------------------------------------

def _count_io_on(target_path: Path):
    """Patch Path.read_text/write_text to count calls on a specific path.

    Returns a context manager that yields a dict {"reads": N, "writes": N}.
    """
    real_read = Path.read_text
    real_write = Path.write_text
    counts = {"reads": 0, "writes": 0}

    def counting_read(self, *args, **kwargs):
        if self == target_path:
            counts["reads"] += 1
        return real_read(self, *args, **kwargs)

    def counting_write(self, *args, **kwargs):
        if self == target_path:
            counts["writes"] += 1
        return real_write(self, *args, **kwargs)

    return (
        patch.object(Path, "read_text", counting_read),
        patch.object(Path, "write_text", counting_write),
        counts,
    )


# ---------------------------------------------------------------------------
# add_components_bulk
# ---------------------------------------------------------------------------

def test_add_components_bulk_writes_all_in_one_pass(sch_with_device_r: Path):
    ops = FileSchematicOps()
    components = [
        {"lib_id": "Device:R", "reference": f"R{i}", "value": f"{i}k",
         "x": 100.0 + i * 10, "y": 50.0}
        for i in range(1, 6)
    ]

    read_p, write_p, counts = _count_io_on(sch_with_device_r)
    with read_p, write_p:
        result = ops.add_components_bulk(sch_with_device_r, components)

    assert counts["reads"] == 1
    assert counts["writes"] == 1
    assert len(result["placed"]) == 5
    assert result["failed"] == []

    content = sch_with_device_r.read_text(encoding="utf-8")
    for ref in ("R1", "R2", "R3", "R4", "R5"):
        assert f'"Reference" "{ref}"' in content


def test_add_components_bulk_dedups_lib_symbols(sch_with_device_r: Path):
    ops = FileSchematicOps()
    components = [
        {"lib_id": "Device:R", "reference": f"R{i}", "value": "10k",
         "x": 100.0 + i * 10, "y": 50.0}
        for i in range(1, 6)
    ]
    ops.add_components_bulk(sch_with_device_r, components)

    content = sch_with_device_r.read_text(encoding="utf-8")
    # Exactly one Device:R definition in lib_symbols (the one we seeded).
    assert content.count('(symbol "Device:R"') == 1
    # Five instances using the cached lib_id.
    assert content.count('(lib_id "Device:R")') == 5


def test_add_components_bulk_partial_success(sch_with_device_r: Path):
    ops = FileSchematicOps()
    components = [
        {"lib_id": "Device:R", "reference": "R1", "value": "10k", "x": 100, "y": 50},
        {"lib_id": "", "reference": "R2", "value": "5k", "x": 110, "y": 50},  # bad
        {"lib_id": "Device:R", "reference": "R3", "value": "1k", "x": 120, "y": 50},
    ]
    result = ops.add_components_bulk(sch_with_device_r, components)

    assert len(result["placed"]) == 2
    assert {p["reference"] for p in result["placed"]} == {"R1", "R3"}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["index"] == 1


def test_add_components_bulk_empty_list(sch_with_device_r: Path):
    ops = FileSchematicOps()
    read_p, write_p, counts = _count_io_on(sch_with_device_r)
    with read_p, write_p:
        result = ops.add_components_bulk(sch_with_device_r, [])

    # Read always happens (read once); write skipped when there are no blocks.
    assert counts["reads"] == 1
    assert counts["writes"] == 0
    assert result == {"placed": [], "failed": []}


# ---------------------------------------------------------------------------
# add_power_symbols_bulk
# ---------------------------------------------------------------------------

def test_add_power_symbols_bulk_writes_all_in_one_pass(sch_with_power_libs: Path):
    ops = FileSchematicOps()
    symbols = [
        {"name": "VCC", "x": 100.0, "y": 30.0},
        {"name": "GND", "x": 110.0, "y": 30.0, "rotation": 180.0},
        {"name": "VCC", "x": 120.0, "y": 30.0},
    ]
    read_p, write_p, counts = _count_io_on(sch_with_power_libs)
    with read_p, write_p:
        result = ops.add_power_symbols_bulk(sch_with_power_libs, symbols)

    assert counts["reads"] == 1
    assert counts["writes"] == 1
    assert len(result["placed"]) == 3
    assert result["failed"] == []


def test_add_power_symbols_bulk_increments_pwr_refs(sch_with_power_libs: Path):
    ops = FileSchematicOps()
    symbols = [
        {"name": "VCC", "x": 100.0, "y": 30.0},
        {"name": "VCC", "x": 110.0, "y": 30.0},
        {"name": "VCC", "x": 120.0, "y": 30.0},
    ]
    result = ops.add_power_symbols_bulk(sch_with_power_libs, symbols)

    refs = [p["reference"] for p in result["placed"]]
    assert refs == ["#PWR001", "#PWR002", "#PWR003"]


def test_add_power_symbols_bulk_resumes_from_existing_max(sch_with_existing_pwr: Path):
    ops = FileSchematicOps()
    symbols = [
        {"name": "VCC", "x": 100.0, "y": 30.0},
        {"name": "VCC", "x": 110.0, "y": 30.0},
    ]
    result = ops.add_power_symbols_bulk(sch_with_existing_pwr, symbols)

    refs = [p["reference"] for p in result["placed"]]
    assert refs == ["#PWR008", "#PWR009"]


# ---------------------------------------------------------------------------
# connect_pins_bulk
# ---------------------------------------------------------------------------

def test_snap_to_grid_helper():
    from kicad_mcp.backends.file_backend import _is_on_grid, _snap_to_grid

    assert _snap_to_grid(96.52) == 96.52          # already on grid
    assert _snap_to_grid(94.92) == 95.25          # rounds to nearest multiple
    assert _snap_to_grid(0.635) == 1.27           # half rounds away from zero
    assert _snap_to_grid(-0.635) == -1.27
    assert _snap_to_grid(0.0) == 0.0
    assert _is_on_grid(50.8)
    assert not _is_on_grid(50.0)

def test_connect_pins_places_label_at_each_pin(sch_with_r1: Path):
    """stub_length=0 → label at exact pin coord, no wire emitted."""
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_r1, ["R1.1", "R1.2"], net="VCC", stub_length=0,
    )

    assert result["failed"] == []
    assert len(result["connected"]) == 2

    by_pin = {c["pin"]: c for c in result["connected"]}
    # R1 is at (100, 50). Pin 1 is at lib (-2.54, 0) → schematic (97.46, 50).
    # Pin 2 is at lib (2.54, 0) → schematic (102.54, 50).
    assert by_pin["R1.1"]["x"] == pytest.approx(97.46)
    assert by_pin["R1.1"]["y"] == pytest.approx(50.0)
    assert by_pin["R1.1"]["wire_uuid"] is None
    assert by_pin["R1.2"]["x"] == pytest.approx(102.54)
    assert by_pin["R1.2"]["wire_uuid"] is None

    content = sch_with_r1.read_text(encoding="utf-8")
    assert content.count('(label "VCC"') == 2
    # No wires were emitted (only the existing schematic content matters here).
    assert "(wire " not in content


def test_connect_pins_with_stub_generates_wire_and_label(sch_with_r1: Path):
    """stub_length=2.54 → wire from pin to snapped stub end + label there.

    R1's origin (100, 50) is off the 1.27 mm grid, so the stub's free end
    snaps to the nearest grid point — (94.92, 50) → (95.25, 49.53) — and an
    off_grid_pin warning fires (#9). The pin-attached end never moves.
    """
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_r1, ["R1.1"], net="GND", stub_length=2.54,
    )

    assert result["failed"] == []
    assert len(result["connected"]) == 1
    entry = result["connected"][0]

    # Pin 1 outward direction is -X (pin at 97.46, symbol center at 100).
    assert entry["x"] == pytest.approx(95.25)
    assert entry["y"] == pytest.approx(49.53)
    assert entry["wire_uuid"] is not None
    assert entry["label_uuid"] is not None

    content = sch_with_r1.read_text(encoding="utf-8")
    assert "(wire (pts (xy 97.46 50.0) (xy 95.25 49.53))" in content
    assert '(label "GND" (at 95.25 49.53 0)' in content

    assert [w["type"] for w in result["warnings"]] == ["off_grid_pin"]
    assert result["warnings"][0]["pin"] == "R1.1"
    assert "move_schematic_component" in result["warnings"][0]["message"]


@pytest.fixture
def sch_with_on_grid_r(tmp_path: Path) -> Path:
    # (101.6, 50.8) = (80, 40) × 1.27 → both pins land on grid too.
    return _make_schematic(
        tmp_path, TEST_R_LIB_SYMBOL,
        instances=_resistor_instance("R5", 101.6, 50.8),
    )


def test_connect_pins_on_grid_pin_unchanged_no_warning(sch_with_on_grid_r: Path):
    """On-grid pin + grid-multiple stub → exact axis-aligned stub, no warning."""
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_on_grid_r, ["R5.1"], net="GND", stub_length=2.54,
    )

    assert result["failed"] == []
    entry = result["connected"][0]
    # Pin 1 at (99.06, 50.8); stub -X 2.54 → (96.52, 50.8). Already on grid.
    assert entry["x"] == pytest.approx(96.52)
    assert entry["y"] == pytest.approx(50.8)
    assert "warnings" not in result

    content = sch_with_on_grid_r.read_text(encoding="utf-8")
    assert "(wire (pts (xy 99.06 50.8) (xy 96.52 50.8))" in content


def test_connect_pins_off_grid_pin_attached_end_stays_on_pin(sch_with_r1: Path):
    """The pin-attached end must equal the pin position exactly (#9)."""
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_r1, ["R1.2"], net="SIG", stub_length=2.54,
    )

    entry = result["connected"][0]
    content = sch_with_r1.read_text(encoding="utf-8")
    # Pin 2 at (102.54, 50); wire starts exactly there.
    assert "(xy 102.54 50.0)" in content
    # Free end on the 1.27 mm grid in both axes.
    assert entry["x"] / 1.27 == pytest.approx(round(entry["x"] / 1.27))
    assert entry["y"] / 1.27 == pytest.approx(round(entry["y"] / 1.27))
    assert [w["type"] for w in result["warnings"]] == ["off_grid_pin"]


def test_connect_pins_short_stub_never_collapses_onto_pin(sch_with_on_grid_r: Path):
    """A sub-grid stub must not snap to a zero-length wire."""
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_on_grid_r, ["R5.1"], net="GND", stub_length=0.5,
    )

    entry = result["connected"][0]
    # Snapping 0.5 mm from on-grid (99.06, 50.8) would land back on the pin;
    # the guard keeps one full grid step instead.
    assert (entry["x"], entry["y"]) != (99.06, 50.8)
    assert entry["x"] == pytest.approx(97.79)
    assert entry["y"] == pytest.approx(50.8)


def test_connect_pins_invalid_pin_ref_in_failed_list(sch_with_r1: Path):
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_r1, ["R1.1", "X9.1", "BAD_FORMAT"], net="DATA", stub_length=0,
    )

    # Valid pin still gets connected.
    assert len(result["connected"]) == 1
    assert result["connected"][0]["pin"] == "R1.1"

    # The two bad refs are reported per-item.
    failed_pins = {f["pin"] for f in result["failed"]}
    assert failed_pins == {"X9.1", "BAD_FORMAT"}


def test_connect_pins_writes_in_one_pass(sch_with_r1: Path):
    ops = FileSchematicOps()
    read_p, write_p, counts = _count_io_on(sch_with_r1)
    with read_p, write_p:
        ops.connect_pins_bulk(
            sch_with_r1, ["R1.1", "R1.2"], net="SIG", stub_length=2.54,
        )

    # Architectural contract: one read, one write, regardless of pin count.
    assert counts["reads"] == 1
    assert counts["writes"] == 1


# ---------------------------------------------------------------------------
# add_no_connects_bulk
# ---------------------------------------------------------------------------

def test_add_no_connects_bulk_writes_in_one_pass(sch_with_device_r: Path):
    ops = FileSchematicOps()
    points = [{"x": 100.0 + i * 5, "y": 50.0} for i in range(4)]

    read_p, write_p, counts = _count_io_on(sch_with_device_r)
    with read_p, write_p:
        result = ops.add_no_connects_bulk(sch_with_device_r, points)

    assert counts["reads"] == 1
    assert counts["writes"] == 1
    assert len(result["placed"]) == 4
    assert result["failed"] == []

    content = sch_with_device_r.read_text(encoding="utf-8")
    assert content.count("(no_connect (at ") == 4


def test_add_no_connects_bulk_partial_success(sch_with_device_r: Path):
    ops = FileSchematicOps()
    points = [
        {"x": 100.0, "y": 50.0},
        {"x": "not_a_number", "y": 50.0},  # invalid
        {"x": 110.0, "y": 50.0},
    ]
    result = ops.add_no_connects_bulk(sch_with_device_r, points)

    assert len(result["placed"]) == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["index"] == 1


def test_add_no_connects_bulk_empty_list(sch_with_device_r: Path):
    ops = FileSchematicOps()
    read_p, write_p, counts = _count_io_on(sch_with_device_r)
    with read_p, write_p:
        result = ops.add_no_connects_bulk(sch_with_device_r, [])

    assert counts["reads"] == 1
    assert counts["writes"] == 0
    assert result == {"placed": [], "failed": []}


# ---------------------------------------------------------------------------
# move_components_bulk
# ---------------------------------------------------------------------------

def test_move_components_bulk_repositions_each(sch_with_three_resistors: Path):
    ops = FileSchematicOps()
    moves = [
        {"reference": "R1", "x": 200.0, "y": 100.0},
        {"reference": "R2", "x": 210.0, "y": 100.0, "rotation": 90.0},
        {"reference": "R3", "x": 220.0, "y": 100.0},
    ]
    result = ops.move_components_bulk(sch_with_three_resistors, moves)

    assert result["failed"] == []
    assert len(result["moved"]) == 3
    by_ref = {m["reference"]: m for m in result["moved"]}
    assert by_ref["R1"]["position"] == {"x": 200.0, "y": 100.0}
    assert by_ref["R2"]["rotation"] == 90.0

    content = sch_with_three_resistors.read_text(encoding="utf-8")
    # R1 symbol-level (at ...) reflects the new position.
    assert "(at 200.0 100.0 0" in content
    assert "(at 210.0 100.0 90.0" in content
    assert "(at 220.0 100.0 0" in content


def test_move_components_bulk_writes_in_one_pass(sch_with_three_resistors: Path):
    ops = FileSchematicOps()
    moves = [
        {"reference": "R1", "x": 200.0, "y": 100.0},
        {"reference": "R2", "x": 210.0, "y": 100.0},
        {"reference": "R3", "x": 220.0, "y": 100.0},
    ]
    read_p, write_p, counts = _count_io_on(sch_with_three_resistors)
    with read_p, write_p:
        ops.move_components_bulk(sch_with_three_resistors, moves)

    assert counts["reads"] == 1
    assert counts["writes"] == 1


def test_move_components_bulk_partial_success(sch_with_three_resistors: Path):
    ops = FileSchematicOps()
    moves = [
        {"reference": "R1", "x": 200.0, "y": 100.0},
        {"reference": "X9", "x": 210.0, "y": 100.0},  # not in schematic
        {"reference": "R3", "x": 220.0, "y": 100.0},
    ]
    result = ops.move_components_bulk(sch_with_three_resistors, moves)

    moved_refs = {m["reference"] for m in result["moved"]}
    assert moved_refs == {"R1", "R3"}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["reference"] == "X9"


def test_move_components_bulk_all_fail_skips_write(sch_with_three_resistors: Path):
    """If every move fails (e.g. all references missing), no file write happens."""
    ops = FileSchematicOps()
    moves = [
        {"reference": "X1", "x": 200.0, "y": 100.0},
        {"reference": "X2", "x": 210.0, "y": 100.0},
        {"reference": "X3", "x": 220.0, "y": 100.0},
    ]
    read_p, write_p, counts = _count_io_on(sch_with_three_resistors)
    with read_p, write_p:
        result = ops.move_components_bulk(sch_with_three_resistors, moves)

    assert counts["reads"] == 1
    assert counts["writes"] == 0
    assert result["moved"] == []
    assert len(result["failed"]) == 3


def test_move_components_bulk_failure_reason_includes_reference(sch_with_three_resistors: Path):
    """The per-item reason must be self-contained — include the reference name."""
    ops = FileSchematicOps()
    result = ops.move_components_bulk(
        sch_with_three_resistors,
        [{"reference": "X9", "x": 10.0, "y": 10.0}],
    )
    assert len(result["failed"]) == 1
    entry = result["failed"][0]
    assert entry["reference"] == "X9"
    # The reason string mentions X9, not just a generic "symbol not found".
    assert "X9" in entry["reason"]


def test_move_components_bulk_preserves_property_layout(sch_with_three_resistors: Path):
    """Property labels must shift by the same delta as the symbol body."""
    ops = FileSchematicOps()
    # R1 was at (100, 50). Move to (200, 100) → delta (100, 50).
    # Original property labels were at (100, 48) and (100, 52);
    # they should now be at (200, 98) and (200, 102).
    ops.move_components_bulk(
        sch_with_three_resistors,
        [{"reference": "R1", "x": 200.0, "y": 100.0}],
    )

    content = sch_with_three_resistors.read_text(encoding="utf-8")
    # Locate R1's symbol block to scope assertions away from R2/R3.
    r1_match = re.search(
        r'\(symbol\s+\(lib_id\s+"Test:R"\)\s+\(at\s+200\.0\s+100\.0[^)]*\).*?Reference"\s+"R1".*?Value"\s+"10k".*?\n  \)\n',
        content, re.DOTALL,
    )
    assert r1_match is not None, "R1 block not found at new position"
    r1_block = r1_match.group(0)
    # Reference label was 2 above the symbol → still 2 above.
    assert "(at 200.0 98.0 0)" in r1_block
    # Value label was 2 below → still 2 below.
    assert "(at 200.0 102.0 0)" in r1_block
