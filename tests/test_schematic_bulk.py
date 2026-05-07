"""Tests for the bulk schematic-capture tools.

Covers FileSchematicOps.add_components_bulk, add_power_symbols_bulk, and
connect_pins_bulk. The bulk methods must perform a single file read +
single file write regardless of input size.
"""

from __future__ import annotations

import textwrap
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
    """stub_length=2.54 → wire from pin to stub end + label at stub end."""
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(
        sch_with_r1, ["R1.1"], net="GND", stub_length=2.54,
    )

    assert result["failed"] == []
    assert len(result["connected"]) == 1
    entry = result["connected"][0]

    # Pin 1 outward direction is -X (pin at 97.46, symbol center at 100).
    # Stub end = (97.46 - 2.54, 50) = (94.92, 50).
    assert entry["x"] == pytest.approx(94.92)
    assert entry["y"] == pytest.approx(50.0)
    assert entry["wire_uuid"] is not None
    assert entry["label_uuid"] is not None

    content = sch_with_r1.read_text(encoding="utf-8")
    assert "(wire (pts (xy 97.46 50.0) (xy 94.92 50.0))" in content
    assert '(label "GND" (at 94.92 50.0 0)' in content


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
