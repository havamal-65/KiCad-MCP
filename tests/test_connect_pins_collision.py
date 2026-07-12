"""REQ-STUB-1..5 (F3/S4, #19) — connect_pins stub-collision safety.

`connect_pins_bulk` used to pick a stub direction by pure cardinal snap, so a
pin's stub could terminate its endpoint — or run its segment — exactly on an
adjacent pin, creating a silent schematic short (the live ESP32 pin 13 → pin 14
USB_DM/DP case). These tests pin the collision check and its fallback ladder:
dominant outward cardinal → perpendiculars (full then one grid step) →
stub_length=0, never a silent short.

Fixtures are self-contained (embedded lib_symbols) so no KiCad install is
needed, and assertions are built from the *resolved* pin positions rather than
hardcoded schematic coords.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import (
    FileSchematicOps,
    _coincident,
    _STUB_COLLISION_EPS_MM,
)

# Symbol origin — on the 1.27 mm grid, so pins at grid-multiple lib offsets
# stay on grid and never trip the off_grid_pin warning.
_CX, _CY = 127.0, 127.0
_EPS = _STUB_COLLISION_EPS_MM


def _pin_def(number: str, lx: float, ly: float) -> str:
    return (
        f'        (pin passive line (at {lx} {ly} 0)\n'
        f'          (length 0)\n'
        f'          (name "~" (effects (font (size 1.27 1.27))))\n'
        f'          (number "{number}" (effects (font (size 1.27 1.27))))\n'
        f'        )\n'
    )


def _make_sch(tmp_path: Path, pins: dict[str, tuple[float, float]]) -> Path:
    """Write a one-symbol schematic ('U1' = Test:U) with the given pins.

    ``pins`` maps pin number → (lib_x, lib_y). With the instance at (127, 127),
    rotation 0, no mirror, the resolver yields schematic (127+lx, 127-ly).
    """
    pin_blocks = "".join(_pin_def(n, lx, ly) for n, (lx, ly) in pins.items())
    lib_symbol = (
        '    (symbol "Test:U" (pin_numbers (hide no)) (pin_names (offset 0))\n'
        "      (exclude_from_sim no) (in_bom yes) (on_board yes)\n"
        '      (symbol "U_0_1"\n'
        f"{pin_blocks}"
        "      )\n"
        "    )\n"
    )
    instance = (
        f'  (symbol (lib_id "Test:U") (at {_CX} {_CY} 0) (unit 1)\n'
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "{_uuid.uuid4()}")\n'
        '    (property "Reference" "U1" (at 127 125 0)\n'
        "      (effects (font (size 1.27 1.27)))\n"
        "    )\n"
        '    (property "Value" "MCU" (at 127 129 0)\n'
        "      (effects (font (size 1.27 1.27)))\n"
        "    )\n"
        "    (instances\n"
        '      (project ""\n'
        '        (path "/11111111-1111-1111-1111-111111111111"\n'
        '          (reference "U1") (unit 1)\n'
        "        )\n"
        "      )\n"
        "    )\n"
        "  )\n"
    )
    content = (
        "(kicad_sch\n"
        "  (version 20250114)\n"
        '  (generator "kicad_mcp")\n'
        '  (generator_version "9.0")\n'
        '  (uuid "11111111-1111-1111-1111-111111111111")\n'
        '  (paper "A4")\n'
        "  (lib_symbols\n"
        f"{lib_symbol}"
        "  )\n"
        f"{instance}"
        "  (sheet_instances\n"
        '    (path "/" (page "1"))\n'
        "  )\n"
        ")\n"
    )
    p = tmp_path / "collision.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


def _resolved(path: Path) -> dict[str, dict[str, float]]:
    ops = FileSchematicOps()
    info = ops.get_symbol_pin_positions(path, "U1")
    assert "error" not in info, info
    return info["pin_positions"]


# ---------------------------------------------------------------------------
# REQ-STUB-1/3 — the endpoint must never land on another pin (ESP32 case)
# ---------------------------------------------------------------------------

def test_endpoint_on_pin_refused(tmp_path: Path):
    # pin 13 sits below symbol center → its dominant stub points +Y straight at
    # pin 14. The old code would land the stub endpoint exactly on pin 14.
    path = _make_sch(tmp_path, {"13": (0.0, -1.27), "14": (0.0, -3.81)})
    pins = _resolved(path)
    p13, p14 = pins["13"], pins["14"]

    # Sanity: the fixture is a genuine trap — the naive +Y 2.54 stub lands on 14.
    naive_end = (round(p13["x"], 4), round(p13["y"] + 2.54, 4))
    assert _coincident(naive_end[0], naive_end[1], p14["x"], p14["y"], _EPS)

    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.13"], net="USB_DP", stub_length=2.54)

    assert result["failed"] == []
    entry = result["connected"][0]
    # REQ-STUB-3 invariant: produced endpoint is NOT coincident with pin 14.
    assert not _coincident(entry["x"], entry["y"], p14["x"], p14["y"], _EPS)


# ---------------------------------------------------------------------------
# REQ-STUB-1 — a stub whose *segment* passes through a pin is rejected
# ---------------------------------------------------------------------------

def test_segment_crosses_pin(tmp_path: Path):
    # pin 1 is left of center → dominant stub points -X. pin 2 sits further left,
    # directly on that segment, so the -X direction must be rejected even though
    # the endpoint itself clears pin 2.
    path = _make_sch(tmp_path, {"1": (-1.27, 0.0), "2": (-2.54, 0.0)})
    pins = _resolved(path)
    p1, p2 = pins["1"], pins["2"]

    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.1"], net="SIG", stub_length=2.54)

    entry = result["connected"][0]
    # It rerouted off the -X axis (endpoint y differs from the pin row).
    assert entry["y"] != pytest.approx(p1["y"])
    # And pin 2 is not on the produced segment.
    from kicad_mcp.backends.file_backend import _point_on_segment
    assert not _point_on_segment(
        p2["x"], p2["y"], p1["x"], p1["y"], entry["x"], entry["y"], _EPS
    )


# ---------------------------------------------------------------------------
# REQ-STUB-2 — no clear direction → stub_length=0 (terminate on pin) + warning
# ---------------------------------------------------------------------------

def test_no_clear_dir_falls_to_zero(tmp_path: Path):
    # Victim pin 5 (dominant +X) with blockers pinning +X, +Y and -Y at both
    # full and one-grid-step length. The inward -X is never tried, so nothing
    # is clear → terminate on the pin.
    path = _make_sch(tmp_path, {
        "5": (2.54, 0.0),      # victim, schem (129.54, 127)
        "b1": (3.81, 0.0),     # blocks +X full (segment) + reduced (endpoint)
        "b2": (2.54, -1.27),   # blocks +Y full + reduced
        "b3": (2.54, 1.27),    # blocks -Y full + reduced
    })
    pins = _resolved(path)
    p5 = pins["5"]

    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.5"], net="NET", stub_length=2.54)

    entry = result["connected"][0]
    # Terminated on the pin — no stub, no wire.
    assert (entry["x"], entry["y"]) == pytest.approx((p5["x"], p5["y"]))
    assert entry["wire_uuid"] is None
    assert "(wire " not in path.read_text(encoding="utf-8")

    warns = [w for w in result.get("warnings", []) if w["type"] == "stub_collision"]
    assert len(warns) == 1
    assert warns[0]["fallback"] == "stub_length=0"


# ---------------------------------------------------------------------------
# REQ-STUB-2 — a clear reduced-length direction is preferred over zero
# ---------------------------------------------------------------------------

def test_reduced_length_preferred_over_zero(tmp_path: Path):
    # Block every FULL-length direction but leave +X clear at one grid step.
    path = _make_sch(tmp_path, {
        "7": (2.54, 0.0),      # victim, dominant +X, schem (129.54, 127)
        "b1": (5.08, 0.0),     # blocks +X full endpoint only (not reduced)
        "b2": (2.54, -2.54),   # blocks +Y full endpoint only
        "b3": (2.54, 2.54),    # blocks -Y full endpoint only
    })
    pins = _resolved(path)
    p7 = pins["7"]

    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.7"], net="NET", stub_length=2.54)

    entry = result["connected"][0]
    # +X reduced: one grid step from the pin, on the pin row.
    assert entry["x"] == pytest.approx(p7["x"] + 1.27)
    assert entry["y"] == pytest.approx(p7["y"])
    assert entry["wire_uuid"] is not None

    warns = [w for w in result.get("warnings", []) if w["type"] == "stub_collision"]
    assert len(warns) == 1
    assert warns[0]["fallback"] == "reduced_length"


# ---------------------------------------------------------------------------
# REQ-STUB-4 — the warning names the conflicting pin(s) and the fallback
# ---------------------------------------------------------------------------

def test_warning_names_conflict(tmp_path: Path):
    path = _make_sch(tmp_path, {
        "5": (2.54, 0.0),
        "b1": (3.81, 0.0),
        "b2": (2.54, -1.27),
        "b3": (2.54, 1.27),
    })
    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.5"], net="NET", stub_length=2.54)

    warn = next(w for w in result["warnings"] if w["type"] == "stub_collision")
    assert warn["pin"] == "U1.5"
    assert warn["fallback"] == "stub_length=0"
    # Names the blocking pins.
    assert set(warn["conflict_pins"]) == {"U1.b1", "U1.b2", "U1.b3"}
    for cp in warn["conflict_pins"]:
        assert cp in warn["message"]


# ---------------------------------------------------------------------------
# REQ-STUB-5 — no regression where there is no collision (#9 output preserved)
# ---------------------------------------------------------------------------

def test_no_regression_no_conflict(tmp_path: Path):
    # Two pins far apart; connecting pin 1 has a clear dominant -X direction.
    path = _make_sch(tmp_path, {"1": (-2.54, 0.0), "2": (2.54, 0.0)})
    pins = _resolved(path)
    p1 = pins["1"]

    ops = FileSchematicOps()
    result = ops.connect_pins_bulk(path, ["U1.1"], net="GND", stub_length=2.54)

    entry = result["connected"][0]
    # Byte-identical to the #9 dominant-cardinal snap: -X by 2.54 on the row.
    from kicad_mcp.backends.file_backend import _snap_to_grid
    assert entry["x"] == pytest.approx(_snap_to_grid(p1["x"] - 2.54))
    assert entry["y"] == pytest.approx(p1["y"])
    assert entry["wire_uuid"] is not None
    # No collision → no stub_collision warning.
    assert not any(
        w["type"] == "stub_collision" for w in result.get("warnings", [])
    )
