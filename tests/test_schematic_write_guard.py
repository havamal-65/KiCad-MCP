"""RW-S4 §9 — schematic open-in-KiCad write guard (NS follow-through).

Every mutating FileSchematicOps method refuses while KiCad holds the target
schematic open (lock file + running process), mirroring the board-side
no-file-write invariant. The refusal surfaces the lock holder, the variant
commit and format upgrade refuse before replacing files, and the tool layer
returns the structured ``schematic_open_in_kicad`` error shape.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import fastmcp
import pytest

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.file_backend import (
    BoardOpenInKiCadError,
    FileSchematicOps,
)
from kicad_mcp.tools.format_upgrade import upgrade_project_files
from kicad_mcp.utils import variant_files as vf

MINIMAL_SCH = "(kicad_sch\n  (version 20231120)\n)\n"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projects" / "variants"
FAKE_CLI = Path(r"C:\fake\kicad-cli.exe")
LOCK_JSON = '{"hostname": "DESKTOP-TEST", "username": "ev"}'

_KICAD_RUNNING = "kicad_mcp.utils.platform_helper.is_kicad_running"

# Every mutating FileSchematicOps method (the §9 inventory): name → args
# after the leading ``path``. The guard fires before any argument is used,
# so representative values suffice.
WRITE_METHOD_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("create_schematic", ()),
    ("annotate", ()),
    ("add_component", ("Device:R", "R1", "10k", 10.0, 10.0)),
    ("add_wire", (0.0, 0.0, 10.0, 0.0)),
    ("add_label", ("VCC", 0.0, 0.0)),
    ("add_no_connect", (1.0, 1.0)),
    ("add_power_symbol", ("GND", 0.0, 0.0)),
    ("add_junction", (1.0, 1.0)),
    ("add_components_bulk",
     ([{"lib_id": "Device:R", "reference": "R1", "value": "1k",
        "x": 0.0, "y": 0.0}],)),
    ("add_power_symbols_bulk", ([{"name": "GND", "x": 0.0, "y": 0.0}],)),
    ("connect_pins_bulk", (["R1.1", "R2.1"], "NET1")),
    ("add_no_connects_bulk", ([{"x": 0.0, "y": 0.0}],)),
    ("move_components_bulk", ([{"reference": "R1", "x": 0.0, "y": 0.0}],)),
    ("move_component", ("R1", 0.0, 0.0)),
    ("update_component_property", ("R1", "Value", "2k")),
    ("remove_component", ("R1",)),
    ("remove_wire", (0.0, 0.0, 10.0, 0.0)),
    ("remove_no_connect", (0.0, 0.0)),
    ("remove_label", (0.0, 0.0)),
    ("set_label_text", (0.0, 0.0, "NEW")),
]


def _locked_sch(tmp_path: Path, lock_content: str = LOCK_JSON) -> Path:
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(MINIMAL_SCH, encoding="utf-8")
    (tmp_path / f"~{sch.name}.lck").write_text(lock_content, encoding="utf-8")
    return sch


def test_write_method_inventory_complete():
    assert len(WRITE_METHOD_CALLS) == 20
    assert len({name for name, _ in WRITE_METHOD_CALLS}) == 20


@pytest.mark.parametrize(
    "method,args", WRITE_METHOD_CALLS, ids=[m for m, _ in WRITE_METHOD_CALLS]
)
def test_all_write_methods_guarded(tmp_path: Path, method: str, args: tuple):
    sch = _locked_sch(tmp_path)
    before = sch.read_bytes()
    ops = FileSchematicOps()
    with patch(_KICAD_RUNNING, return_value=True):
        with pytest.raises(BoardOpenInKiCadError) as exc:
            getattr(ops, method)(sch, *args)
    assert "eeschema" in str(exc.value)
    assert sch.read_bytes() == before


def test_stale_lock_does_not_block(tmp_path: Path):
    """Lock on disk but no KiCad process → a crash leftover; writes proceed."""
    sch = _locked_sch(tmp_path)
    ops = FileSchematicOps()
    with patch(_KICAD_RUNNING, return_value=False):
        result = ops.add_wire(sch, 0.0, 0.0, 10.0, 0.0)
    assert "uuid" in result
    assert "(wire" in sch.read_text(encoding="utf-8")


def test_lock_holder_parsed_and_surfaced(tmp_path: Path):
    sch = _locked_sch(tmp_path)
    exc = BoardOpenInKiCadError(sch, tmp_path / f"~{sch.name}.lck")
    assert exc.lock_holder == {"hostname": "DESKTOP-TEST", "username": "ev"}


def test_lock_holder_null_on_bad_json(tmp_path: Path):
    sch = _locked_sch(tmp_path, lock_content="not json at all")
    exc = BoardOpenInKiCadError(sch, tmp_path / f"~{sch.name}.lck")
    assert exc.lock_holder is None
    # Missing lock file is equally non-fatal.
    exc2 = BoardOpenInKiCadError(sch, tmp_path / "~missing.lck")
    assert exc2.lock_holder is None


def test_board_message_keeps_remedy_sentence(tmp_path: Path):
    board_exc = BoardOpenInKiCadError(
        tmp_path / "x.kicad_pcb", tmp_path / "~x.kicad_pcb.lck")
    assert (
        "Board edits must go through the live path while KiCad is open; "
        "close the board in KiCad, or if this lock is stale from a crash, "
        "delete it."
    ) in str(board_exc)
    sch_exc = BoardOpenInKiCadError(
        tmp_path / "x.kicad_sch", tmp_path / "~x.kicad_sch.lck")
    assert (
        "Schematic edits are file-based; close the schematic in eeschema "
        "and retry."
    ) in str(sch_exc)


# ── variant write session ────────────────────────────────────────────────────

class _FakeDRCOps:
    """Oracle stand-in; never reached when only the .kicad_pro is staged."""

    def __init__(self, cli_path: Path) -> None:
        self.cli = cli_path


@pytest.fixture
def variant_project(tmp_path: Path) -> Path:
    for name in ("shift_reg_v1.kicad_pro", "shift_reg_v1.kicad_sch",
                 "shift_reg_v1.kicad_pcb"):
        shutil.copy2(FIXTURE_DIR / name, tmp_path / name)
    return tmp_path / "shift_reg_v1.kicad_pro"


def test_variant_scratch_write_not_guarded(variant_project: Path):
    """Staging is in-memory + scratch-dir only — the guard belongs to the
    final replace, so a held lock must not refuse the authoring stage."""
    files = vf.resolve_project(variant_project)
    lock = variant_project.parent / f"~{variant_project.name}.lck"
    lock.write_text(LOCK_JSON, encoding="utf-8")
    session = vf.VariantWriteSession(files)
    staged = vf.pro_upsert_variant(session.texts[files.pro], "Pro", None)
    with patch(_KICAD_RUNNING, return_value=True):
        assert session.stage(files.pro, staged) is True


def test_variant_apply_guarded(variant_project: Path):
    files = vf.resolve_project(variant_project)
    before = variant_project.read_text(encoding="utf-8")
    session = vf.VariantWriteSession(files)
    staged = vf.pro_upsert_variant(session.texts[files.pro], "Pro", None)
    assert session.stage(files.pro, staged)
    lock = variant_project.parent / f"~{variant_project.name}.lck"
    lock.write_text(LOCK_JSON, encoding="utf-8")
    with patch("kicad_mcp.backends.cli_backend.CLIDRCOps", _FakeDRCOps), \
         patch(_KICAD_RUNNING, return_value=True):
        with pytest.raises(vf.VariantWriteError) as exc:
            session.commit(FAKE_CLI)
    payload = exc.value.payload
    assert payload["error"] == "schematic_open_in_kicad"
    assert payload["lock_file"] == str(lock)
    assert payload["lock_holder"] == {
        "hostname": "DESKTOP-TEST", "username": "ev"}
    assert "open in KiCad" in payload["message"]
    # all-or-nothing: original untouched, scratch cleaned up
    assert variant_project.read_text(encoding="utf-8") == before
    assert not list(variant_project.parent.glob(".variant_write_tmp_*"))


# ── format upgrade ───────────────────────────────────────────────────────────

def test_format_upgrade_guarded(tmp_path: Path):
    (tmp_path / "old.kicad_pro").write_text("{}", encoding="utf-8")
    sch = tmp_path / "old.kicad_sch"
    sch.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    (tmp_path / f"~{sch.name}.lck").write_text(LOCK_JSON, encoding="utf-8")
    files = vf.resolve_project(tmp_path / "old.kicad_pro")
    before = sch.read_text(encoding="utf-8")
    with patch(_KICAD_RUNNING, return_value=True):
        result = upgrade_project_files(files, FAKE_CLI)
    assert result["status"] == "error"
    assert result["error_code"] == "schematic_open_in_kicad"
    assert result["lock_file"] == str(tmp_path / f"~{sch.name}.lck")
    assert result["lock_holder"] == {
        "hostname": "DESKTOP-TEST", "username": "ev"}
    assert sch.read_text(encoding="utf-8") == before


# ── tool layer ───────────────────────────────────────────────────────────────

class _Backend(BackendProtocol):
    def get_schematic_ops(self):
        return FileSchematicOps()

    def get_schematic_modify_ops(self):
        return FileSchematicOps()


def _tool(tmp_path: Path, name: str):
    from kicad_mcp.tools import schematic
    from kicad_mcp.utils.change_log import ChangeLog

    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, _Backend(), ChangeLog(tmp_path / "c.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == name)


def test_tool_layer_error_code_shape(tmp_path: Path):
    sch = _locked_sch(tmp_path)
    with patch(_KICAD_RUNNING, return_value=True):
        result = json.loads(
            _tool(tmp_path, "add_wire")(str(sch), 0.0, 0.0, 10.0, 0.0))
    assert result["status"] == "error"
    assert result["error_code"] == "schematic_open_in_kicad"
    assert result["lock_file"] == str(tmp_path / f"~{sch.name}.lck")
    assert result["lock_holder"] == {
        "hostname": "DESKTOP-TEST", "username": "ev"}
    assert "eeschema" in result["message"]


# ── source scan ──────────────────────────────────────────────────────────────

def test_no_unguarded_write_text():
    """Any FileSchematicOps method that writes the schematic must guard first
    — catches a future mutating method added without the §9 guard."""
    source = textwrap.dedent(inspect.getsource(FileSchematicOps))
    cls = ast.parse(source).body[0]
    assert isinstance(cls, ast.ClassDef)
    unguarded = []
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        seg = ast.unparse(node)
        if "path.write_text(" in seg and "_refuse_if_open_in_kicad(" not in seg:
            unguarded.append(node.name)
    assert unguarded == []
