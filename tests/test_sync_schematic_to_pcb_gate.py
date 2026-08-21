"""Tests for the §6.2 sync precondition gate (REQ-TEST-003).

sync_schematic_to_pcb must refuse — before any PCB work — when the
symbol-footprint validator reports an unfixable mismatch or unresolvable
footprint. No backup, no placement, no net assignment may happen.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fastmcp

from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation_cache import record_validation

_PASS = {
    "passed": True, "checked": 0, "mismatches": [],
    "unresolvable": [], "warnings": [], "over_limit": False,
}

_FAIL = {
    "passed": False,
    "checked": 1,
    "mismatches": [{
        "ref": "U1", "footprint": "Pkg:Bad",
        "symbol_pins": ["1", "2", "3", "4", "5"],
        "footprint_pads": ["1", "2", "3", "4"],
        "missing": ["5"],
    }],
    "unresolvable": [],
    "warnings": [],
    "over_limit": False,
}


def _sync_fn(backend, change_log):
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, backend, change_log)
    return next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "sync_schematic_to_pcb"
    )


def test_sync_refuses_on_unresolvable_footprint(tmp_path: Path):
    """KIR4 NS: the hard sync gate is footprint resolvability (KiCad's netlist
    proves symbol-side validity; the pin↔pad check lives in the standalone
    validate_symbol_footprint_pairs tool)."""
    from tests.netlist_stub import DrcOpsStub, render_netlist

    sch = tmp_path / "p.kicad_sch"
    pcb = tmp_path / "p.kicad_pcb"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    pcb.write_text("(kicad_pcb)", encoding="utf-8")

    backend = MagicMock()
    backend.get_drc_ops.return_value = DrcOpsStub(render_netlist([
        {"ref": "U1", "value": "X", "footprint": "Pkg:Bad"},
    ]))
    tool_fn = _sync_fn(backend, ChangeLog(tmp_path / "changes.json"))

    with patch(
        "kicad_mcp.utils.fp_lib_table.get_footprint_library_map",
        return_value={},
    ):
        result = json.loads(tool_fn(str(sch), str(pcb), apply_footprint_changes=True))

    assert result["status"] == "blocked", result
    assert result["reason"] == "footprint_unresolvable", result
    assert result["unresolvable"][0]["ref"] == "U1"

    # The gate fires before any board work — board ops must never be touched.
    backend.get_board_ops.assert_not_called()
    backend.get_board_modify_ops.assert_not_called()


# ── §6.3 soft gate — warn when validate_schematic_for_pcb hasn't passed ───────

def _empty_backend():
    from tests.netlist_stub import DrcOpsStub, render_netlist

    backend = MagicMock()
    backend.get_schematic_ops.return_value.read_schematic.return_value = {"symbols": []}
    backend.get_board_ops.return_value.get_components.return_value = []
    backend.get_drc_ops.return_value = DrcOpsStub(render_netlist([]))
    return backend


def test_sync_warns_when_schematic_not_validated(tmp_path: Path):
    sch = tmp_path / "p.kicad_sch"
    pcb = tmp_path / "p.kicad_pcb"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    tool_fn = _sync_fn(_empty_backend(), ChangeLog(tmp_path / "changes.json"))

    result = json.loads(tool_fn(str(sch), str(pcb)))

    warn_types = [w.get("type") for w in result.get("warnings", [])]
    assert "validate_schematic_for_pcb_not_passed" in warn_types, result


def test_sync_no_gate_warning_after_validation(tmp_path: Path):
    sch = tmp_path / "p.kicad_sch"
    pcb = tmp_path / "p.kicad_pcb"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    # Schematic was validated against its current content — gate is satisfied.
    record_validation(sch, "validate_schematic_for_pcb", {"passed": True})
    tool_fn = _sync_fn(_empty_backend(), ChangeLog(tmp_path / "changes.json"))

    result = json.loads(tool_fn(str(sch), str(pcb)))

    warn_types = [w.get("type") for w in result.get("warnings", [])]
    assert "validate_schematic_for_pcb_not_passed" not in warn_types, result
