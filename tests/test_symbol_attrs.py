"""Tests for symbol attribute authoring (F-DV12, spec-symbol-attrs).

Covers the four optional flags on add_component / add_components_bulk
(in_bom, on_board, dnp, exclude_from_sim), the new set_symbol_attributes
edit path, and the read_schematic attr read-back — all mocked at the file
boundary, no KiCad needed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import fastmcp
import pytest

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.utils.change_log import ChangeLog


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

# Symbol entry for a generic Device:R — presence in lib_symbols is
# enough to short-circuit _ensure_lib_symbol_cached. The cached definition
# deliberately carries its own (in_bom yes) token (foundation 8). The unit
# sub-symbols (R_0_1 body, R_1_1 pins) are required by kicad-skip, whose
# LibSymbol.__init__ accesses a child (symbol ...) unconditionally — a bare
# entry crashes it and silently sends read_schematic down the sexp fallback
# (RB-RES3).
DEVICE_R_LIB_SYMBOL = """\
    (symbol "Device:R" (pin_numbers (hide no)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
      )
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
      )
    )
"""

# Today's hardcoded attr line — the byte-identical-default reference.
DEFAULT_ATTR_LINE = "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"


def _instance(
    ref: str,
    lib: str = "Device:R",
    unit: int = 1,
    attr_line: str = DEFAULT_ATTR_LINE,
    uuid: str = "22222222-2222-2222-2222-222222222222",
) -> str:
    """A placed symbol block in today's add_component layout, with a
    configurable attr line (empty string = tokens absent)."""
    return (
        f'  (symbol (lib_id "{lib}") (at 100 50 0) (unit {unit})\n'
        f'{attr_line}'
        f'    (uuid "{uuid}")\n'
        f'    (property "Reference" "{ref}" (at 100 48 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Value" "10k" (at 100 52 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (instances\n'
        f'      (project ""\n'
        f'        (path "/11111111-1111-1111-1111-111111111111"\n'
        f'          (reference "{ref}") (unit {unit})\n'
        f'        )\n'
        f'      )\n'
        f'    )\n'
        f'  )\n'
    )


def _make_schematic(tmp_path: Path, instances: str = "") -> Path:
    sch = (
        SCH_HEADER
        + "  (lib_symbols\n"
        + DEVICE_R_LIB_SYMBOL
        + "  )\n\n"
        + instances
        + SCH_FOOTER
    )
    f = tmp_path / "test.kicad_sch"
    f.write_text(sch, encoding="utf-8")
    return f


K10_FIXTURE = Path(__file__).parent / "fixtures" / "boards" / "k10" / "k10_saved.kicad_sch"


# ---------------------------------------------------------------------------
# C1 — add_component flags
# ---------------------------------------------------------------------------

def test_add_component_default_attr_line_byte_identical(tmp_path: Path):
    # Builder leg: no new params ⇒ output equals the pre-change builder's,
    # golden string built with today's literal.
    block = FileSchematicOps._build_symbol_block(
        "Device:R", "R1", "10k", 100, 50, 0.0,
        None, "", None, "aaaa-uuid", "ssss-uuid",
    )
    golden = (
        '  (symbol (lib_id "Device:R") (at 100 50 0.0) (unit 1)\n'
        + DEFAULT_ATTR_LINE
        + '    (uuid "aaaa-uuid")\n'
        '    (property "Reference" "R1" (at 100 48 0)\n'
        '      (effects (font (size 1.27 1.27)))\n'
        '    )\n'
        '    (property "Value" "10k" (at 100 52 0)\n'
        '      (effects (font (size 1.27 1.27)))\n'
        '    )\n'
        '    (instances\n'
        '      (project ""\n'
        '        (path "/ssss-uuid"\n'
        '          (reference "R1") (unit 1)\n'
        '        )\n'
        '      )\n'
        '    )\n'
        '  )\n'
    )
    assert block == golden

    # File leg: the written file contains exactly today's default line,
    # and the result payload carries none of the four keys.
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    result = ops.add_component(sch, "Device:R", "R1", "10k", 100, 50)
    content = sch.read_text(encoding="utf-8")
    assert DEFAULT_ATTR_LINE in content
    for key in ("in_bom", "on_board", "dnp", "exclude_from_sim"):
        assert key not in result


def test_add_component_in_bom_false(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    result = ops.add_component(
        sch, "Device:R", "R1", "10k", 100, 50, in_bom=False,
    )
    content = sch.read_text(encoding="utf-8")
    assert "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)\n" in content
    assert result["in_bom"] is False


def test_add_component_all_four_flags(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    ops.add_component(
        sch, "Device:R", "R1", "10k", 100, 50,
        in_bom=False, on_board=False, dnp=True, exclude_from_sim=True,
    )
    content = sch.read_text(encoding="utf-8")
    assert "(exclude_from_sim yes) (in_bom no) (on_board no) (dnp yes)" in content


def test_add_component_rejects_non_bool(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    with pytest.raises(ValueError) as excinfo:
        ops.add_component(sch, "Device:R", "R1", "10k", 100, 50, in_bom="no")
    assert str(excinfo.value) == "in_bom must be a boolean (true/false), got 'no'"


# ---------------------------------------------------------------------------
# C2 — add_components_bulk per-item flags
# ---------------------------------------------------------------------------

def test_add_components_bulk_per_item_attrs(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    result = ops.add_components_bulk(sch, [
        {"lib_id": "Device:R", "reference": "R1", "value": "10k",
         "x": 100.0, "y": 50.0, "dnp": True},
        {"lib_id": "Device:R", "reference": "R2", "value": "5k",
         "x": 110.0, "y": 50.0},
    ])
    assert [p["reference"] for p in result["placed"]] == ["R1", "R2"]
    content = sch.read_text(encoding="utf-8")
    r1_blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "R1")
    r2_blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "R2")
    r1_block = content[r1_blocks[0]["start"]:r1_blocks[0]["end"] + 1]
    r2_block = content[r2_blocks[0]["start"]:r2_blocks[0]["end"] + 1]
    assert "(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp yes)" in r1_block
    assert DEFAULT_ATTR_LINE in r2_block


def test_add_components_bulk_non_bool_fails_item_only(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()

    real_write = Path.write_text
    writes = {"count": 0}

    def counting_write(self, *args, **kwargs):
        if self == sch:
            writes["count"] += 1
        return real_write(self, *args, **kwargs)

    with patch.object(Path, "write_text", counting_write):
        result = ops.add_components_bulk(sch, [
            {"lib_id": "Device:R", "reference": "R1", "value": "10k",
             "x": 100.0, "y": 50.0, "in_bom": "nope"},
            {"lib_id": "Device:R", "reference": "R2", "value": "5k",
             "x": 110.0, "y": 50.0},
        ])

    assert writes["count"] == 1
    assert len(result["failed"]) == 1
    assert result["failed"][0]["index"] == 0
    assert result["failed"][0]["reason"] == (
        "in_bom must be a boolean (true/false), got 'nope'"
    )
    assert [p["reference"] for p in result["placed"]] == ["R2"]
    content = sch.read_text(encoding="utf-8")
    assert FileSchematicOps._find_symbol_blocks_by_reference(content, "R2")
    assert not FileSchematicOps._find_symbol_blocks_by_reference(content, "R1")


# ---------------------------------------------------------------------------
# C3 — set_symbol_attributes
# ---------------------------------------------------------------------------

def test_set_symbol_attributes_replaces_existing_tokens(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    ops.add_component(sch, "Device:R", "R1", "10k", 100, 50)

    result = ops.set_symbol_attributes(
        sch, "R1", in_bom=False, exclude_from_sim=True,
    )

    content = sch.read_text(encoding="utf-8")
    assert "    (exclude_from_sim yes) (in_bom no) (on_board yes) (dnp no)\n" in content
    assert result["attributes"] == {
        "in_bom": False, "on_board": True,
        "dnp": False, "exclude_from_sim": True,
    }


def test_set_symbol_attributes_inserts_missing_token(tmp_path: Path):
    no_dnp_line = "    (exclude_from_sim no) (in_bom yes) (on_board yes)\n"
    sch = _make_schematic(tmp_path, instances=_instance("R1", attr_line=no_dnp_line))
    ops = FileSchematicOps()

    result = ops.set_symbol_attributes(sch, "R1", dnp=True)

    content = sch.read_text(encoding="utf-8")
    # Inserted on its own line immediately before (uuid ", matching indent.
    assert (
        no_dnp_line
        + '    (dnp yes)\n'
        + '    (uuid "22222222-2222-2222-2222-222222222222")\n'
    ) in content
    assert result["attributes"]["dnp"] is True


def test_set_symbol_attributes_multi_unit_all_blocks(tmp_path: Path):
    instances = (
        _instance("U1", lib="Device:R", unit=1,
                  uuid="22222222-2222-2222-2222-222222222222")
        + _instance("U1", lib="Device:R", unit=2,
                    uuid="33333333-3333-3333-3333-333333333333")
    )
    sch = _make_schematic(tmp_path, instances=instances)
    ops = FileSchematicOps()

    result = ops.set_symbol_attributes(sch, "U1", dnp=True)

    assert result["units_updated"] == [1, 2]
    content = sch.read_text(encoding="utf-8")
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "U1")
    assert len(blocks) == 2
    for blk in blocks:
        assert "(dnp yes)" in content[blk["start"]:blk["end"] + 1]


def test_set_symbol_attributes_skips_lib_symbols(tmp_path: Path):
    sch = _make_schematic(tmp_path, instances=_instance("R1"))
    ops = FileSchematicOps()

    ops.set_symbol_attributes(sch, "R1", in_bom=False)

    content = sch.read_text(encoding="utf-8")
    # The cached library definition's (in_bom yes) is byte-unchanged.
    assert DEVICE_R_LIB_SYMBOL in content
    blocks = FileSchematicOps._find_symbol_blocks_by_reference(content, "R1")
    assert "(in_bom no)" in content[blocks[0]["start"]:blocks[0]["end"] + 1]


def test_set_symbol_attributes_unknown_reference(tmp_path: Path):
    sch = _make_schematic(tmp_path, instances=_instance("R1"))
    ops = FileSchematicOps()
    with pytest.raises(ValueError) as excinfo:
        ops.set_symbol_attributes(sch, "X9", dnp=True)
    assert str(excinfo.value) == f"Symbol with reference 'X9' not found in {sch}"


def test_set_symbol_attributes_requires_one_attr(tmp_path: Path):
    sch = _make_schematic(tmp_path, instances=_instance("R1"))
    ops = FileSchematicOps()
    with pytest.raises(ValueError) as excinfo:
        ops.set_symbol_attributes(sch, "R1")
    assert str(excinfo.value) == (
        "set_symbol_attributes: nothing to set — pass at least one "
        "of in_bom, on_board, dnp, exclude_from_sim"
    )


def test_set_symbol_attributes_k10_multiline_grammar(tmp_path: Path):
    sch = tmp_path / "k10_saved.kicad_sch"
    shutil.copy2(K10_FIXTURE, sch)
    before = sch.read_text(encoding="utf-8")
    ops = FileSchematicOps()

    result = ops.set_symbol_attributes(sch, "C1", in_bom=False)

    after = sch.read_text(encoding="utf-8")
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines)
    diffs = [
        (old, new)
        for old, new in zip(before_lines, after_lines)
        if old != new
    ]
    # Only that token's value word changes (K10 one-token-per-line layout).
    assert len(diffs) == 1
    old_line, new_line = diffs[0]
    assert old_line.strip() == "(in_bom yes)"
    assert new_line.strip() == "(in_bom no)"
    assert new_line.replace("no", "yes", 1) == old_line
    assert result["attributes"]["in_bom"] is False


# ---------------------------------------------------------------------------
# C4 — read_schematic attr read-back (skip and sexp paths must agree)
# ---------------------------------------------------------------------------

def _symbol_row(read_result: dict, ref: str) -> dict:
    return next(
        s for s in read_result["symbols"] if s.get("reference") == ref
    )


def _read_with_skip_strict(ops: FileSchematicOps, sch: Path) -> dict:
    """The kicad-skip read leg with NO silent fallback (RB-RES3).

    read_schematic() falls back to the sexp parser on any skip exception,
    so an equivalence test built on it can silently compare sexp against
    sexp. This parses with skip.Schematic directly — a skip crash fails
    the test instead of hiding behind the fallback.
    """
    skip = pytest.importorskip("skip")
    return ops._read_with_skip(skip.Schematic(str(sch)), sch)


def test_read_schematic_roundtrip_attrs(tmp_path: Path):
    sch = _make_schematic(tmp_path)
    ops = FileSchematicOps()
    ops.add_component(
        sch, "Device:R", "R1", "10k", 100, 50, in_bom=False, dnp=True,
    )

    for read_result in (_read_with_skip_strict(ops, sch), ops._read_with_sexp(sch)):
        row = _symbol_row(read_result, "R1")
        assert row["in_bom"] is False
        assert row["dnp"] is True
        assert row["on_board"] is True
        assert row["exclude_from_sim"] is False


def test_read_schematic_attr_defaults_when_absent(tmp_path: Path):
    # All four tokens stripped from the placed block.
    sch = _make_schematic(tmp_path, instances=_instance("R1", attr_line=""))
    ops = FileSchematicOps()

    for read_result in (_read_with_skip_strict(ops, sch), ops._read_with_sexp(sch)):
        row = _symbol_row(read_result, "R1")
        assert row["in_bom"] is True
        assert row["on_board"] is True
        assert row["dnp"] is False
        assert row["exclude_from_sim"] is False


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

class _Backend(BackendProtocol):
    def get_schematic_ops(self):
        return FileSchematicOps()

    def get_schematic_modify_ops(self):
        return FileSchematicOps()


def _tool(tmp_path: Path, name: str):
    from kicad_mcp.tools import schematic

    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, _Backend(), ChangeLog(tmp_path / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == name)


def test_set_symbol_attributes_refused_when_open_in_kicad(tmp_path: Path):
    sch = _make_schematic(tmp_path, instances=_instance("R1"))
    (tmp_path / f"~{sch.name}.lck").write_text("{}", encoding="utf-8")

    with patch(
        "kicad_mcp.utils.platform_helper.is_kicad_running", return_value=True,
    ):
        result = json.loads(
            _tool(tmp_path, "set_symbol_attributes")(str(sch), "R1", dnp=True)
        )

    assert result["status"] == "error"
    assert result["error_code"] == "schematic_open_in_kicad"
    # File untouched.
    assert "(dnp yes)" not in sch.read_text(encoding="utf-8")


def test_tool_set_symbol_attributes_change_log_and_backup(tmp_path: Path):
    sch = _make_schematic(tmp_path, instances=_instance("R1"))

    result = json.loads(
        _tool(tmp_path, "set_symbol_attributes")(
            str(sch), "R1", in_bom=False, exclude_from_sim=True,
        )
    )

    assert result["status"] == "success"
    entries = [
        json.loads(line)
        for line in (tmp_path / "changes.json").read_text(
            encoding="utf-8").strip().splitlines()
    ]
    entry = next(e for e in entries if e["tool"] == "set_symbol_attributes")
    # Only the non-None flags are recorded.
    assert entry["params"] == {
        "path": str(sch), "reference": "R1",
        "in_bom": False, "exclude_from_sim": True,
    }
    assert entry["backup_path"]
    assert Path(entry["backup_path"]).exists()
