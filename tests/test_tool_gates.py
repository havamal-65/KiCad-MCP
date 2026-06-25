"""Tests for the §6.3 set-time tool gates (utils/gates.py + wiring).

The gate utility generalizes the autoroute "must have run the validator first"
pattern onto the sidecar validation cache:
- export_gerbers HARD-refuses until run_drc has passed (terminal op).
- sync_schematic_to_pcb WARNS (non-blocking) when validate_schematic_for_pcb
  hasn't passed for the current schematic content (iterative op).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fastmcp
import pytest

from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.gates import check_gate, refuse_if_ungated, warn_if_ungated
from kicad_mcp.utils.validation_cache import record_validation


def _file(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("(kicad_pcb (version 1))", encoding="utf-8")
    return p


# ── utils/gates.py — check_gate ──────────────────────────────────────────────

def test_check_gate_not_run(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    gap = check_gate(p, "run_drc")
    assert gap == {"ran": False, "passed": False, "violations": []}


def test_check_gate_ran_but_failed(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    record_validation(p, "run_drc", {"passed": False, "violations": [{"x": 1}]})
    gap = check_gate(p, "run_drc")
    assert gap == {"ran": True, "passed": False, "violations": [{"x": 1}]}


def test_check_gate_passed_returns_none(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    record_validation(p, "run_drc", {"passed": True})
    assert check_gate(p, "run_drc") is None


def test_check_gate_invalidated_by_content_change(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    record_validation(p, "run_drc", {"passed": True})
    assert check_gate(p, "run_drc") is None
    p.write_text("(kicad_pcb (version 2))", encoding="utf-8")  # board changed
    assert check_gate(p, "run_drc") == {"ran": False, "passed": False, "violations": []}


# ── refuse_if_ungated / warn_if_ungated ──────────────────────────────────────

def test_refuse_if_ungated_blocks_then_passes(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    refusal = refuse_if_ungated(p, "run_drc", "export_gerbers", fix_hint="Run DRC.")
    assert refusal is not None
    parsed = json.loads(refusal)
    assert parsed["status"] == "blocked"
    assert parsed["reason"] == "run_drc_gate"
    assert "has not been run" in parsed["message"]

    record_validation(p, "run_drc", {"passed": True})
    assert refuse_if_ungated(p, "run_drc", "export_gerbers") is None


def test_warn_if_ungated_warns_then_clears(tmp_path: Path):
    p = _file(tmp_path, "s.kicad_sch")
    warning = warn_if_ungated(p, "validate_schematic_for_pcb", "sync_schematic_to_pcb")
    assert warning is not None
    assert warning["type"] == "validate_schematic_for_pcb_not_passed"

    record_validation(p, "validate_schematic_for_pcb", {"passed": True})
    assert warn_if_ungated(p, "validate_schematic_for_pcb", "sync_schematic_to_pcb") is None


# ── export_gerbers hard gate (run_drc) ───────────────────────────────────────

def _export_fn(backend, change_log_dir: Path):
    from kicad_mcp.tools import export
    mcp = fastmcp.FastMCP("test")
    export.register_tools(mcp, backend, ChangeLog(change_log_dir / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "export_gerbers")


def test_export_gerbers_refuses_without_drc(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    backend = MagicMock()
    tool_fn = _export_fn(backend, tmp_path)

    result = json.loads(tool_fn(str(p), str(tmp_path / "out")))
    assert result["status"] == "blocked", result
    assert result["reason"] == "run_drc_gate"
    backend.get_export_ops.assert_not_called()  # never attempted the export


def test_export_gerbers_proceeds_after_drc_passes(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    record_validation(p, "run_drc", {"passed": True})

    backend = MagicMock()
    backend.get_export_ops.return_value.export_gerbers.return_value = {"files": ["F.Cu.gbr"]}
    tool_fn = _export_fn(backend, tmp_path)

    result = json.loads(tool_fn(str(p), str(tmp_path / "out")))
    assert result["status"] == "success", result
    backend.get_export_ops.return_value.export_gerbers.assert_called_once()


def test_export_gerbers_refuses_when_drc_failed(tmp_path: Path):
    p = _file(tmp_path, "b.kicad_pcb")
    record_validation(p, "run_drc", {"passed": False, "violations": [{"type": "clearance"}]})

    backend = MagicMock()
    tool_fn = _export_fn(backend, tmp_path)

    result = json.loads(tool_fn(str(p), str(tmp_path / "out")))
    assert result["status"] == "blocked", result
    assert "did not pass" in result["message"]
    backend.get_export_ops.assert_not_called()
