"""REQ-FE-1/2 (KIR3 S5, F8) — version-drift warning on known 9->10 pin renames.

When `connect_pins_bulk` misses a pin because the symbol renamed it between
KiCad 9 and 10 (the evidenced case: every USB_C_Receptacle* variant renamed
its shield pin S1 -> SH), the failure must name the new pin, carry a
structured `known_rename` + `suggested_pin`, and double-surface as a
`pin_rename_9_to_10` warning — while NEVER auto-substituting: nothing is
written for the failed pin.

Fixtures are self-contained (embedded lib_symbols, same style as
test_connect_pins_collision.py) so no KiCad install is needed.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

from kicad_mcp.backends.file_backend import FileSchematicOps

# Symbol origin on the 1.27 mm grid so pins never trip the off_grid_pin warning.
_CX, _CY = 127.0, 127.0


def _pin_def(number: str, lx: float, ly: float) -> str:
    return (
        f'        (pin passive line (at {lx} {ly} 0)\n'
        f'          (length 0)\n'
        f'          (name "~" (effects (font (size 1.27 1.27))))\n'
        f'          (number "{number}" (effects (font (size 1.27 1.27))))\n'
        f'        )\n'
    )


def _make_sch(
    tmp_path: Path,
    pins: dict[str, tuple[float, float]],
    lib_id: str = "Connector:USB_C_Receptacle_USB2.0_16P",
    reference: str = "J1",
) -> Path:
    """Write a one-symbol schematic with the given pins under *lib_id*."""
    pin_blocks = "".join(_pin_def(n, lx, ly) for n, (lx, ly) in pins.items())
    lib_symbol = (
        f'    (symbol "{lib_id}" (pin_numbers (hide no)) (pin_names (offset 0))\n'
        "      (exclude_from_sim no) (in_bom yes) (on_board yes)\n"
        '      (symbol "SYM_0_1"\n'
        f"{pin_blocks}"
        "      )\n"
        "    )\n"
    )
    instance = (
        f'  (symbol (lib_id "{lib_id}") (at {_CX} {_CY} 0) (unit 1)\n'
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "{_uuid.uuid4()}")\n'
        f'    (property "Reference" "{reference}" (at 127 125 0)\n'
        "      (effects (font (size 1.27 1.27)))\n"
        "    )\n"
        '    (property "Value" "USB_C" (at 127 129 0)\n'
        "      (effects (font (size 1.27 1.27)))\n"
        "    )\n"
        "    (instances\n"
        '      (project ""\n'
        '        (path "/11111111-1111-1111-1111-111111111111"\n'
        f'          (reference "{reference}") (unit 1)\n'
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
    p = tmp_path / "rename.kicad_sch"
    p.write_text(content, encoding="utf-8")
    return p


def _usb_c_sch(tmp_path: Path) -> Path:
    """A KiCad-10-style USB-C symbol: shield pin is SH (S1 does not exist)."""
    return _make_sch(tmp_path, {"SH": (0.0, 2.54), "A1": (0.0, -2.54)})


# ---------------------------------------------------------------------------
# REQ-FE-1 — the failure names the rename and suggests the new pin
# ---------------------------------------------------------------------------

def test_renamed_pin_failure_is_enriched(tmp_path: Path):
    path = _usb_c_sch(tmp_path)
    before = path.read_text(encoding="utf-8")
    ops = FileSchematicOps()

    result = ops.connect_pins_bulk(path, ["J1.S1"], "GND")

    assert result["connected"] == []
    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["pin"] == "J1.S1"
    assert "'SH'" in failure["reason"]
    assert "KiCad" in failure["reason"]
    assert "Not auto-substituted" in failure["reason"]
    assert failure["suggested_pin"] == "J1.SH"
    assert failure["known_rename"]["from"] == "S1"
    assert failure["known_rename"]["to"] == "SH"
    assert failure["known_rename"]["lib_id"] == "Connector:USB_C_Receptacle_USB2.0_16P"

    # REQ-FE-2 double-surface: a warnings[] entry a warnings-only scanner sees.
    warnings = result.get("warnings", [])
    rename_warnings = [w for w in warnings if w["type"] == "pin_rename_9_to_10"]
    assert len(rename_warnings) == 1
    assert rename_warnings[0]["pin"] == "J1.S1"
    assert rename_warnings[0]["suggested_pin"] == "J1.SH"

    # REQ-FE-2 no silent connect: nothing was written to the schematic.
    after = path.read_text(encoding="utf-8")
    assert after == before
    assert "(label" not in after
    assert "(wire" not in after


def test_renamed_pin_new_name_connects_clean(tmp_path: Path):
    path = _usb_c_sch(tmp_path)
    ops = FileSchematicOps()

    result = ops.connect_pins_bulk(path, ["J1.SH"], "GND")

    assert result["failed"] == []
    assert len(result["connected"]) == 1
    assert result["connected"][0]["pin"] == "J1.SH"
    warnings = result.get("warnings", [])
    assert [w for w in warnings if w["type"] == "pin_rename_9_to_10"] == []
    assert '(label "GND"' in path.read_text(encoding="utf-8")


def test_genuinely_absent_pin_gets_bare_reason(tmp_path: Path):
    # Z9 is not a rename — the failure must stay the plain "not found" shape
    # with no misleading rename hint.
    path = _usb_c_sch(tmp_path)
    ops = FileSchematicOps()

    result = ops.connect_pins_bulk(path, ["J1.Z9"], "GND")

    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["reason"] == "pin 'Z9' not found on 'J1'"
    assert "known_rename" not in failure
    assert "suggested_pin" not in failure
    assert [w for w in result.get("warnings", [])
            if w["type"] == "pin_rename_9_to_10"] == []


def test_existence_gate_suppresses_hint_when_new_pin_absent(tmp_path: Path):
    # A prefix-matching symbol that carries NEITHER S1 nor SH (wrong symbol):
    # the rename lookup matches, but the existence gate must suppress the
    # hint — suggesting 'SH' would point at another pin that isn't there.
    path = _make_sch(tmp_path, {"A1": (0.0, 2.54), "B1": (0.0, -2.54)})
    ops = FileSchematicOps()

    result = ops.connect_pins_bulk(path, ["J1.S1"], "GND")

    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["reason"] == "pin 'S1' not found on 'J1'"
    assert "known_rename" not in failure
    assert "suggested_pin" not in failure
    assert [w for w in result.get("warnings", [])
            if w["type"] == "pin_rename_9_to_10"] == []


def test_mixed_batch_rename_does_not_block_other_pins(tmp_path: Path):
    # One renamed miss + one good pin in the same batch: the good pin still
    # connects, the miss is enriched, batch status semantics unchanged.
    path = _usb_c_sch(tmp_path)
    ops = FileSchematicOps()

    result = ops.connect_pins_bulk(path, ["J1.S1", "J1.A1"], "GND")

    assert [c["pin"] for c in result["connected"]] == ["J1.A1"]
    assert [f["pin"] for f in result["failed"]] == ["J1.S1"]
    assert result["failed"][0]["suggested_pin"] == "J1.SH"
    content = path.read_text(encoding="utf-8")
    assert content.count('(label "GND"') == 1
