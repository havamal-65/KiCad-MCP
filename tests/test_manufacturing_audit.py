"""Tests for the §6.7 manufacturing-readiness audit (REQ-TEST-001…006).

The audit orchestrates the four pre-fab checks (DRC, board-size, courtyard,
3D-models) and the five fab artifacts (gerbers, drill, BOM, P&P, STEP) into a
single ``ready_to_ship`` verdict. These tests mock the backend DRC/export ops and
patch the importable check impls (imported inside the audit, so patching the
source module is honoured at call time).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp
import pytest

from kicad_mcp.tools import board as board_mod
from kicad_mcp.tools import drc as drc_mod
from kicad_mcp.tools import export as export_mod
from kicad_mcp.tools import manufacturing
from kicad_mcp.tools.manufacturing import run_manufacturing_readiness_audit
from kicad_mcp.utils.change_log import ChangeLog

# ── canned check verdicts ────────────────────────────────────────────────────

_DRC_PASS = {"passed": True, "error_count": 0, "warning_count": 2, "violations": []}
_DRC_FAIL = {"passed": False, "error_count": 3, "warning_count": 0,
             "violations": [{"severity": "error", "type": "clearance"}]}

_BS_PASS = {"passed": True, "shortfall_breakdown": {"required_mm2": 100.0},
            "suggested_min_dimensions": {"width_mm": 40.0, "height_mm": 30.0},
            "warnings": []}
_BS_FAIL = {"passed": False,
            "shortfall_breakdown": {"required_mm2": 500.0, "usable_mm2": 196.0,
                                    "shortfall_mm2": 304.0},
            "suggested_min_dimensions": {"width_mm": 55.0, "height_mm": 45.0},
            "warnings": []}

_CO_PASS = {"passed": True, "overlap_count": 0, "footprints_checked": 3, "overlaps": []}

_M3D_PASS = {"ready": True, "checked": 5, "missing": [], "warnings": []}
_M3D_MISSING = {"ready": False, "checked": 5,
                "missing": [{"footprint": "U1", "ref": "U1",
                             "model_path": "${KICAD9_3DMODEL_DIR}/x.step",
                             "reason": "file_not_found"}],
                "warnings": []}


def _export_ok() -> MagicMock:
    """A backend export-ops mock whose every export succeeds."""
    ops = MagicMock()
    ops.export_gerbers.return_value = {"success": True, "output_dir": "g",
                                       "output_files": ["g/a-F_Cu.gbr", "g/a.drl"]}
    ops.export_drill.return_value = {"success": True, "output_dir": "d", "output_files": ["d/a.drl"]}
    ops.export_bom.return_value = {"success": True, "output_files": ["bom.csv"]}
    ops.export_pick_and_place.return_value = {"success": True, "output_files": ["pos.csv"]}
    ops.export_step.return_value = {"success": True, "output_file": "model.step"}
    return ops


def _backend(drc_result: dict, export_ops: MagicMock | None = None) -> MagicMock:
    backend = MagicMock()
    backend.get_drc_ops.return_value.run_drc.return_value = drc_result
    backend.get_export_ops.return_value = export_ops if export_ops is not None else _export_ok()
    return backend


def _patch_checks(monkeypatch, *, bs=_BS_PASS, co=_CO_PASS, m3d=_M3D_PASS) -> None:
    monkeypatch.setattr(board_mod, "run_verify_board_size", lambda p, **k: bs)
    monkeypatch.setattr(drc_mod, "run_check_courtyard_overlaps", lambda p: co)
    monkeypatch.setattr(export_mod, "run_verify_3d_models", lambda p: m3d)


def _board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    return p


# ── REQ-TEST-001 — all clear ─────────────────────────────────────────────────

def test_all_clear_ready_to_ship(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _backend(_DRC_PASS)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    assert result["ready_to_ship"] is True, result
    assert result["blocking_issues"] == []
    assert len(result["artifacts"]) == 5
    assert all(a["generated"] for a in result["artifacts"])
    assert {a["name"] for a in result["artifacts"]} == {"gerbers", "drill", "bom", "pos", "step"}


# ── REQ-TEST-002 — DRC fail gates artifacts ──────────────────────────────────

def test_drc_fail_gates_artifacts(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _backend(_DRC_FAIL)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    assert result["ready_to_ship"] is False
    backend.get_export_ops.assert_not_called()
    assert len(result["artifacts"]) == 5
    assert all(a["generated"] is False for a in result["artifacts"])
    assert all(a["detail"]["skipped"] == "drc_failed" for a in result["artifacts"])
    drc_blocks = [b for b in result["blocking_issues"] if b.get("check") == "drc"]
    assert len(drc_blocks) == 1
    assert drc_blocks[0]["detail"]["errors"] == 3


# ── REQ-TEST-003 — board-size fail (artifacts still attempted) ───────────────

def test_board_size_fail_blocks_but_exports_run(tmp_path, monkeypatch):
    _patch_checks(monkeypatch, bs=_BS_FAIL)
    backend = _backend(_DRC_PASS)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    assert result["ready_to_ship"] is False
    bs_blocks = [b for b in result["blocking_issues"] if b.get("check") == "board_size"]
    assert len(bs_blocks) == 1
    assert bs_blocks[0]["detail"]["suggested_min_dimensions"]["width_mm"] == 55.0
    # DRC passed → exports were attempted and generated.
    backend.get_export_ops.assert_called()
    assert all(a["generated"] for a in result["artifacts"])


# ── REQ-TEST-004 — missing 3D model is advisory ──────────────────────────────

def test_missing_3d_model_is_advisory(tmp_path, monkeypatch):
    _patch_checks(monkeypatch, m3d=_M3D_MISSING)
    backend = _backend(_DRC_PASS)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    assert result["ready_to_ship"] is True, result
    assert any(a["type"] == "missing_3d_model" for a in result["advisories"])
    assert not any(b.get("check") == "verify_3d_models" for b in result["blocking_issues"])
    step = next(a for a in result["artifacts"] if a["name"] == "step")
    assert step["generated"] is True


# ── REQ-TEST-005 — per-artifact isolation ────────────────────────────────────

def test_one_export_raises_others_still_run(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    ops = _export_ok()
    ops.export_bom.side_effect = RuntimeError("kicad-cli BOM boom")
    backend = _backend(_DRC_PASS, export_ops=ops)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    bom = next(a for a in result["artifacts"] if a["name"] == "bom")
    assert bom["generated"] is False
    assert "BOM boom" in bom["detail"]["error"]
    assert any(b.get("artifact") == "bom" for b in result["blocking_issues"])
    # The other four exports were still attempted.
    ops.export_gerbers.assert_called_once()
    ops.export_drill.assert_called_once()
    ops.export_pick_and_place.assert_called_once()
    ops.export_step.assert_called_once()
    assert result["ready_to_ship"] is False


# ── REQ-TEST-006 — skip_artifacts ────────────────────────────────────────────

def test_skip_artifacts(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _backend(_DRC_PASS)

    result = run_manufacturing_readiness_audit(
        backend, _board(tmp_path), tmp_path / "out", skip_artifacts=True,
    )

    assert result["artifacts"] == []
    backend.get_export_ops.assert_not_called()
    assert result["ready_to_ship"] is False
    assert any(b.get("artifact") == "*" for b in result["blocking_issues"])
    # Checks still ran.
    assert {c["name"] for c in result["checks"]} == {
        "drc", "board_size", "courtyard_overlaps", "verify_3d_models",
    }


# ── check isolation (REQ-CHECK-005) ──────────────────────────────────────────

def test_check_isolation_drc_raises(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = MagicMock()
    backend.get_drc_ops.return_value.run_drc.side_effect = RuntimeError("no kicad-cli")

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    drc_check = next(c for c in result["checks"] if c["name"] == "drc")
    assert drc_check["passed"] is False
    assert "no kicad-cli" in drc_check["detail"]["error"]
    # DRC didn't pass → artifacts gated as drc_failed, other checks still ran.
    assert any(b.get("check") == "drc" for b in result["blocking_issues"])
    assert all(a["detail"]["skipped"] == "drc_failed" for a in result["artifacts"])
    assert result["ready_to_ship"] is False


# ── high_utilization warning surfaces as advisory ────────────────────────────

def test_high_utilization_warning_is_advisory(tmp_path, monkeypatch):
    bs = {**_BS_PASS, "warnings": [{"type": "high_utilization", "utilization_pct": 92.0}]}
    _patch_checks(monkeypatch, bs=bs)
    backend = _backend(_DRC_PASS)

    result = run_manufacturing_readiness_audit(backend, _board(tmp_path), tmp_path / "out")

    assert result["ready_to_ship"] is True
    assert any(a["type"] == "high_utilization" for a in result["advisories"])


# ── tool wrapper registers + returns JSON ────────────────────────────────────

def test_tool_registered_and_returns_json(tmp_path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _backend(_DRC_PASS)
    mcp = fastmcp.FastMCP("test")
    manufacturing.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "manufacturing_readiness_audit"
    )

    out = tool_fn(str(_board(tmp_path)), str(tmp_path / "out"))
    parsed = json.loads(out)
    assert parsed["ready_to_ship"] is True
    assert "checks" in parsed and "artifacts" in parsed
