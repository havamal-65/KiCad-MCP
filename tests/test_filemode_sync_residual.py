"""SW-1 / F-REC4 file-mode residual (spec-filemode-sync-residual).

File-mode sync must resolve project-local footprints and never drop or
delete a part: C1 per-call project-dir derivation inside ``FileBoardOps``,
C2 the four consumer sites, C3 lookup-naming refusals, C4 the file-mode
footprint-swap rollback. Every test here runs the REAL fp-lib-table
resolver against a tmp project (no ``_load_kicad_mod`` patching) unless it
exercises a failure injection.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import fastmcp
import pytest

from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps
from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

# ---------------------------------------------------------------------------
# Tmp-project scaffolding: fp-lib-table + ${KIPRJMOD} Footprints.pretty —
# the ecc83-recreation layout the residual breaks on (spec probe 10).
# ---------------------------------------------------------------------------

_FP_LIB_TABLE = textwrap.dedent("""\
    (fp_lib_table
      (version 7)
      (lib (name "Footprints")(type "KiCad")(uri "${KIPRJMOD}/Footprints.pretty")(options "")(descr ""))
    )
""")

_MOD_TEMPLATE = textwrap.dedent("""\
    (footprint "{name}"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (property "Reference" "REF**" (at 0 -1.65 0) (layer "F.SilkS"))
      (property "Value" "{name}" (at 0 1.65 0) (layer "F.Fab"))
      (fp_rect (start -1.7 -1.2) (end 1.7 1.2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd roundrect (at -0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "2" smd roundrect (at 0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask"))
    )
""")

_EMPTY_BOARD = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
    )
""")

_BOARD_OLDPART = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "VCC")
      (net 2 "GND")
      (footprint "Footprints:OldPart"
        (layer "F.Cu")
        (at 50 60 90)
        (uuid "aaaaaaaa-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 -1.5 0)
          (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (property "Value" "10k" (at 0 1.5 0)
          (layer "F.Fab")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu")
          (net 1 "VCC")
        )
        (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu")
          (net 2 "GND")
        )
      )
    )
""")

_SCH_TEMPLATE = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "{reference}" (at 100 47 0))
        (property "Value" "{value}" (at 100 53 0))
        (property "Footprint" "{footprint}" (at 100 50 0))
      )
    )
""")


def _make_project(
    root: Path, fp_names: list[str], *,
    board_text: str = _EMPTY_BOARD, name: str = "proj",
) -> tuple[Path, Path]:
    """A tmp KiCad project: fp-lib-table + Footprints.pretty + a board."""
    proj = root / name
    proj.mkdir()
    (proj / "fp-lib-table").write_text(_FP_LIB_TABLE, encoding="utf-8")
    pretty = proj / "Footprints.pretty"
    pretty.mkdir()
    for fp_name in fp_names:
        (pretty / f"{fp_name}.kicad_mod").write_text(
            _MOD_TEMPLATE.format(name=fp_name), encoding="utf-8")
    board = proj / f"{name}.kicad_pcb"
    board.write_text(board_text, encoding="utf-8")
    return proj, board


def _ref_block(content: str, reference: str) -> str:
    location = find_footprint_block_by_reference(content, reference)
    assert location is not None, f"{reference} not on the board"
    start, end = location
    return content[start:end + 1]


# ---------------------------------------------------------------------------
# C1/C2 — per-call project-dir derivation
# ---------------------------------------------------------------------------

def test_file_board_ops_derives_project_dir_from_board_path(tmp_path: Path):
    """A bare FileBoardOps() (the router's probe-1 construction) resolves a
    project-local footprint from the board's own directory."""
    _proj, board = _make_project(tmp_path, ["LocalPart"])
    ops = FileBoardOps()
    result = ops.place_component(board, "U1", "Footprints:LocalPart", 10.0, 10.0)
    assert result["reference"] == "U1"
    block = _ref_block(board.read_text(encoding="utf-8"), "U1")
    assert '(footprint "Footprints:LocalPart"' in block
    # The fixture's real pads, not a stub.
    assert '(pad "1"' in block
    assert '(pad "2"' in block


def test_explicit_project_dir_overrides_derivation(tmp_path: Path):
    """A constructor project_dir pointing at a different project wins over
    path.parent."""
    _proj_a, board = _make_project(tmp_path, ["PartA"], name="proj_a")
    proj_b, _board_b = _make_project(tmp_path, ["PartB"], name="proj_b")

    # PartB is invisible from the board's own project...
    with pytest.raises(ValueError):
        FileBoardOps().place_component(board, "U9", "Footprints:PartB", 10.0, 10.0)

    # ...but the explicit constructor project_dir (project B) wins.
    ops = FileBoardOps(project_dir=proj_b)
    result = ops.place_component(board, "U1", "Footprints:PartB", 10.0, 10.0)
    assert result["reference"] == "U1"
    block = _ref_block(board.read_text(encoding="utf-8"), "U1")
    assert '(footprint "Footprints:PartB"' in block
    assert '(pad "1"' in block


def test_bulk_place_project_local_without_project_dir(tmp_path: Path):
    _proj, board = _make_project(tmp_path, ["LocalPart", "OtherPart"])
    ops = FileBoardOps()
    result = ops.place_components_bulk(board, [
        {"reference": "U1", "footprint": "Footprints:LocalPart",
         "x": 10.0, "y": 10.0},
        {"reference": "U2", "footprint": "Footprints:OtherPart",
         "x": 30.0, "y": 10.0},
    ])
    assert result["placed"] == ["U1", "U2"]
    assert result["failed"] == []
    content = board.read_text(encoding="utf-8")
    assert '(pad "1"' in _ref_block(content, "U1")
    assert '(pad "1"' in _ref_block(content, "U2")


# ---------------------------------------------------------------------------
# C3 — unresolvable footprints name the lookup (REQ-NS-4)
# ---------------------------------------------------------------------------

def test_place_unresolvable_error_names_footprint_and_lookup(tmp_path: Path):
    _proj, board = _make_project(tmp_path, ["LocalPart"])
    with pytest.raises(ValueError) as exc_info:
        FileBoardOps().place_component(board, "X1", "NoSuchLib:Nope", 5.0, 5.0)
    msg = str(exc_info.value)
    assert "NoSuchLib:Nope" in msg
    assert str(board.parent) in msg
    assert "stub placement removed" in msg


def test_bulk_unresolvable_failed_entry_names_lookup(tmp_path: Path):
    _proj, board = _make_project(tmp_path, ["LocalPart"])
    result = FileBoardOps().place_components_bulk(board, [
        {"reference": "X1", "footprint": "NoSuchLib:Nope", "x": 5.0, "y": 5.0},
    ])
    assert result["placed"] == []
    assert len(result["failed"]) == 1
    reason = result["failed"][0]["reason"]
    assert "NoSuchLib:Nope" in reason
    assert str(board.parent) in reason
    assert "stub placement removed" in reason


# ---------------------------------------------------------------------------
# Router file leg (probe-1 construction site), end-to-end
# ---------------------------------------------------------------------------

def test_router_file_leg_resolves_project_local(tmp_path: Path, monkeypatch):
    """PluginDirectBackend with IPC unavailable, bridge down, KiCad closed
    serves a FileBoardOps that places a project-local footprint."""
    from kicad_mcp.backends.plugin_backend import (
        BridgeTemporarilyUnavailableError,
    )
    from kicad_mcp_plugin.backends import plugin_direct
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

    _proj, board = _make_project(tmp_path, ["LocalPart"])

    def no_bridge(method, timeout, **kwargs):
        raise BridgeTemporarilyUnavailableError("no bridge in tests")

    monkeypatch.setattr(plugin_direct, "_tcp_call", no_bridge)
    backend = PluginDirectBackend()
    backend._bridge_available = False
    monkeypatch.setattr(backend, "_probe_bridge", lambda: False)
    monkeypatch.setattr(backend._ipc, "is_available", lambda: False)
    monkeypatch.setattr(plugin_direct, "is_kicad_running", lambda: False)

    ops = backend.get_board_modify_ops()
    assert isinstance(ops, FileBoardOps)
    result = ops.place_components_bulk(board, [
        {"reference": "U1", "footprint": "Footprints:LocalPart",
         "x": 10.0, "y": 10.0},
    ])
    assert result["placed"] == ["U1"]
    assert result["failed"] == []
    assert '(pad "1"' in _ref_block(board.read_text(encoding="utf-8"), "U1")


# ---------------------------------------------------------------------------
# File-mode sync end-to-end
# ---------------------------------------------------------------------------

def _make_sync_backend(netlist_text: str):
    """Backend whose modify ops are the router's project-blind FileBoardOps()
    (probe 1) and whose netlist comes from the shared DrcOpsStub."""
    from tests.netlist_stub import DrcOpsStub

    from kicad_mcp.backends.base import BackendProtocol

    class _Backend(BackendProtocol):
        def get_board_ops(self):
            return FileBoardOps()

        def get_board_modify_ops(self):
            return FileBoardOps()

        def get_schematic_ops(self):
            return FileSchematicOps()

        def get_schematic_modify_ops(self):
            return FileSchematicOps()

        def get_drc_ops(self):
            return DrcOpsStub(netlist_text)

    return _Backend()


def _sync_tool(netlist_text: str, tmp: Path):
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(
        mcp, _make_sync_backend(netlist_text), ChangeLog(tmp / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values()
                if t.name == "sync_schematic_to_pcb")


def test_sync_file_mode_places_project_local_parts(tmp_path: Path):
    """File-mode sync on a project-local footprint: success, no place_failed
    warnings, real pads in the written .kicad_pcb."""
    from tests.netlist_stub import render_netlist

    proj, board = _make_project(tmp_path, ["LocalPart"])
    sch = proj / "proj.kicad_sch"
    sch.write_text(_SCH_TEMPLATE.format(
        reference="U1", value="X1", footprint="Footprints:LocalPart"),
        encoding="utf-8")
    netlist = render_netlist(
        [{"ref": "U1", "value": "X1", "footprint": "Footprints:LocalPart"}],
        [{"name": "N1", "nodes": [("U1", "1"), ("U1", "2")]}],
    )
    tool_fn = _sync_tool(netlist, proj)
    result = json.loads(tool_fn(str(sch), str(board)))

    assert result["status"] == "success"
    assert [w for w in result["warnings"]
            if w.get("type") == "place_failed"] == []
    assert result["summary"]["components_placed"] == 1
    block = _ref_block(board.read_text(encoding="utf-8"), "U1")
    assert '(pad "1"' in block
    assert '(pad "2"' in block


# ---------------------------------------------------------------------------
# C4 — file-mode footprint-swap: probe-5 trigger green + rollback guard
# ---------------------------------------------------------------------------

def test_swap_file_mode_project_local_succeeds(tmp_path: Path):
    """The exact probe-5 trigger (project-local new footprint through the
    router's bare FileBoardOps) now applies instead of deleting the part."""
    _proj, board = _make_project(
        tmp_path, ["OldPart", "NewPart"], board_text=_BOARD_OLDPART)
    swap = schematic._attempt_footprint_swap(
        board, "R1", "Footprints:NewPart", board_ops=FileBoardOps())
    assert swap["applied"] is True
    assert swap["via"] == "file"
    content = board.read_text(encoding="utf-8")
    assert "Footprints:OldPart" not in content
    block = _ref_block(content, "R1")
    assert '(footprint "Footprints:NewPart"' in block
    assert '(pad "1"' in block
    assert '(pad "2"' in block


def test_remove_component_returns_raw_block(tmp_path: Path):
    _proj, board = _make_project(
        tmp_path, ["OldPart"], board_text=_BOARD_OLDPART)
    before = board.read_text(encoding="utf-8")
    location = find_footprint_block_by_reference(before, "R1")
    assert location is not None
    start, end = location
    expected = before[start:end + 1]

    result = FileBoardOps().remove_component(board, "R1")
    assert result["removed"] is True
    assert result["raw_block"] == expected
    assert expected not in board.read_text(encoding="utf-8")


def test_restore_footprint_block_reinserts_verbatim(tmp_path: Path):
    _proj, board = _make_project(
        tmp_path, ["OldPart"], board_text=_BOARD_OLDPART)
    pads_before = board.read_text(encoding="utf-8").count("(pad ")

    ops = FileBoardOps()
    removed = ops.remove_component(board, "R1")
    assert find_footprint_block_by_reference(
        board.read_text(encoding="utf-8"), "R1") is None

    result = ops.restore_footprint_block(board, removed["raw_block"])
    assert result == {"restored": True}
    after = board.read_text(encoding="utf-8")
    assert removed["raw_block"] in after
    assert after.count("(pad ") == pads_before
    assert find_footprint_block_by_reference(after, "R1") is not None


def test_swap_rollback_restores_part_on_place_failure(tmp_path: Path):
    """place_component raising after the remove rolls the part back."""
    _proj, board = _make_project(
        tmp_path, ["OldPart", "NewPart"], board_text=_BOARD_OLDPART)
    with patch.object(FileBoardOps, "place_component",
                      side_effect=RuntimeError("place exploded")):
        swap = schematic._attempt_footprint_swap(
            board, "R1", "Footprints:NewPart", board_ops=FileBoardOps())
    assert swap["applied"] is False
    assert swap["restored"] is True
    assert "original footprint restored (rollback)" in swap["reason"]
    content = board.read_text(encoding="utf-8")
    assert find_footprint_block_by_reference(content, "R1") is not None
    assert "Footprints:OldPart" in content


def test_swap_rollback_failure_names_backup(tmp_path: Path):
    """Restore also raising: reason carries ROLLBACK FAILED, restored False,
    and sync emits the swap_rollback_failed warning."""
    from tests.netlist_stub import render_netlist

    # Direct leg — the swap result itself.
    _proj, board = _make_project(
        tmp_path, ["OldPart", "NewPart"], board_text=_BOARD_OLDPART)
    with patch.object(FileBoardOps, "place_component",
                      side_effect=RuntimeError("place exploded")), \
         patch.object(FileBoardOps, "restore_footprint_block",
                      side_effect=OSError("disk full")):
        swap = schematic._attempt_footprint_swap(
            board, "R1", "Footprints:NewPart", board_ops=FileBoardOps())
    assert swap["applied"] is False
    assert swap["restored"] is False
    assert "ROLLBACK FAILED" in swap["reason"]
    assert "sync backup" in swap["reason"]

    # Sync leg — the warning only fires on restored: False.
    proj2, board2 = _make_project(
        tmp_path, ["OldPart", "NewPart"], board_text=_BOARD_OLDPART,
        name="proj2")
    sch = proj2 / "proj2.kicad_sch"
    sch.write_text(_SCH_TEMPLATE.format(
        reference="R1", value="10k", footprint="Footprints:NewPart"),
        encoding="utf-8")
    netlist = render_netlist(
        [{"ref": "R1", "value": "10k", "footprint": "Footprints:NewPart"}],
        [],
    )
    tool_fn = _sync_tool(netlist, proj2)
    with patch.object(FileBoardOps, "place_component",
                      side_effect=RuntimeError("place exploded")), \
         patch.object(FileBoardOps, "restore_footprint_block",
                      side_effect=OSError("disk full")):
        result = json.loads(tool_fn(str(sch), str(board2),
                                    apply_footprint_changes=True))
    rollback_warnings = [w for w in result["warnings"]
                         if w.get("type") == "swap_rollback_failed"]
    assert len(rollback_warnings) == 1
    assert rollback_warnings[0]["reference"] == "R1"
    assert "ROLLBACK FAILED" in rollback_warnings[0]["message"]
    skip = result["footprint_changes_skipped"][0]
    assert "ROLLBACK FAILED" in skip["reason"]


# ---------------------------------------------------------------------------
# C2 side benefit — auto_place (row strategy) sees project-local courtyards
# ---------------------------------------------------------------------------

def test_auto_place_project_local_no_default_bounds(tmp_path: Path):
    _proj, board = _make_project(tmp_path, ["LocalPart"])
    ops = FileBoardOps()
    ops.place_component(board, "R1", "Footprints:LocalPart", 20.0, 20.0)
    result = ops.auto_place(board, strategy="row")
    assert result["components_placed"] == 1
    assert not [w for w in result["warnings"]
                if "not in libraries, using 5×5 mm default" in str(w)]
