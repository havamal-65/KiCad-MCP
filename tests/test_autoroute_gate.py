"""Tests for autoroute connector-orientation hash gate (Phase 6.1.4).

Verifies that autoroute refuses to start when validate_connector_orientations
has not been run, has failed, or was run against a different board version.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _board_with_inward_connector(tmp_path: Path) -> Path:
    """Board with one connector facing inward (south edge, rotation 180)."""
    content = "\n".join([
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
        '  (gr_rect (start 0 0) (end 80 70) (stroke (width 0.05)) (layer "Edge.Cuts"))',
        '  (footprint "Connector_JST:JST_PH_Horizontal" (layer "F.Cu") (at 40 67 180)',
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.Fab"))',
        '    (fp_text user "PCB edge" (at 0 3.65 0) (layer "Dwgs.User"))',
        '  )',
        ")",
    ]) + "\n"
    p = tmp_path / "inward.kicad_pcb"
    p.write_text(content, encoding="utf-8")
    return p


def _board_with_outward_connector(tmp_path: Path) -> Path:
    """Board with one connector facing outward (south edge, rotation 0)."""
    content = "\n".join([
        "(kicad_pcb",
        "  (version 20231231)",
        '  (generator "pcbnew")',
        '  (net 0 "")',
        '  (gr_rect (start 0 0) (end 80 70) (stroke (width 0.05)) (layer "Edge.Cuts"))',
        '  (footprint "Connector_JST:JST_PH_Horizontal" (layer "F.Cu") (at 40 65 0)',
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.Fab"))',
        '    (fp_text user "PCB edge" (at 0 3.65 0) (layer "Dwgs.User"))',
        '  )',
        ")",
    ]) + "\n"
    p = tmp_path / "outward.kicad_pcb"
    p.write_text(content, encoding="utf-8")
    return p


def _call_autoroute(board_path: Path) -> dict:
    """Drive autoroute through the registered MCP tool with mocked backend.

    We never actually run FreeRouting — we only care about the gate, which
    fires before any backend method is invoked.
    """
    import fastmcp
    from kicad_mcp.tools import routing
    from kicad_mcp.utils.change_log import ChangeLog

    backend_stub = MagicMock()
    change_log = ChangeLog(board_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    routing.register_tools(mcp, backend_stub, change_log, config={})
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "autoroute")
    return json.loads(tool_fn(str(board_path)))


# ---------------------------------------------------------------------------
# Gate refuses when validator has never run
# ---------------------------------------------------------------------------

def test_autoroute_refuses_when_no_cache(tmp_path: Path):
    board = _board_with_outward_connector(tmp_path)
    # Do NOT call validate_connector_orientations first
    result = _call_autoroute(board)
    assert result["status"] == "error"
    assert "validate_connector_orientations" in result["message"]
    assert any(
        s["step"] == "connector_orientation_gate" and s["status"] == "error"
        for s in result["steps"]
    )


# ---------------------------------------------------------------------------
# Gate refuses when validator failed
# ---------------------------------------------------------------------------

def test_autoroute_refuses_when_validator_failed(tmp_path: Path):
    from kicad_mcp.tools.drc import run_validate_connector_orientations

    board = _board_with_inward_connector(tmp_path)
    val = run_validate_connector_orientations(board)
    assert val["passed"] is False  # sanity check

    result = _call_autoroute(board)
    assert result["status"] == "error"
    assert "violations" in result
    assert len(result["violations"]) > 0
    assert "place_at_edge" in result["message"]


# ---------------------------------------------------------------------------
# Gate accepts after validator passed
# ---------------------------------------------------------------------------

def test_autoroute_passes_gate_when_validator_passed(tmp_path: Path):
    """When validator passed, the gate lets through — autoroute continues past it.

    The test asserts the gate step is recorded as success, not that the
    overall autoroute call succeeds (it doesn't — FreeRouting isn't installed
    in CI). The gate is the only thing we own here.
    """
    from kicad_mcp.tools.drc import run_validate_connector_orientations

    board = _board_with_outward_connector(tmp_path)
    val = run_validate_connector_orientations(board)
    assert val["passed"] is True  # sanity check

    result = _call_autoroute(board)
    # Gate must report success; downstream may still error (no FreeRouting), but the gate is the test
    assert any(
        s["step"] == "connector_orientation_gate" and s["status"] == "success"
        for s in result["steps"]
    )


# ---------------------------------------------------------------------------
# Hash invalidation: validator passed, then board changed, gate refuses
# ---------------------------------------------------------------------------

def test_autoroute_refuses_when_board_changed_after_validation(tmp_path: Path):
    from kicad_mcp.tools.drc import run_validate_connector_orientations

    board = _board_with_outward_connector(tmp_path)
    val = run_validate_connector_orientations(board)
    assert val["passed"] is True

    # Tamper with the board — invalidates the cached hash
    text = board.read_text(encoding="utf-8")
    board.write_text(text + "\n", encoding="utf-8")

    result = _call_autoroute(board)
    assert result["status"] == "error"
    # Cache miss → message says "has not been run"
    assert "has not been run" in result["message"]


# ---------------------------------------------------------------------------
# Validation cache helpers
# ---------------------------------------------------------------------------

def test_compute_board_hash_changes_with_content(tmp_path: Path):
    from kicad_mcp.utils.validation_cache import compute_board_hash

    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb)", encoding="utf-8")
    h1 = compute_board_hash(p)
    p.write_text("(kicad_pcb )", encoding="utf-8")
    h2 = compute_board_hash(p)
    assert h1 != h2


def test_record_and_get_validation_roundtrip(tmp_path: Path):
    from kicad_mcp.utils.validation_cache import (
        get_validation,
        record_validation,
    )

    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (foo bar))", encoding="utf-8")
    record_validation(p, "validate_connector_orientations", {"passed": True, "checked": 3})
    got = get_validation(p, "validate_connector_orientations")
    assert got is not None
    assert got["passed"] is True
    assert got["checked"] == 3
    assert "ts" in got


def test_get_validation_returns_none_when_hash_mismatched(tmp_path: Path):
    from kicad_mcp.utils.validation_cache import (
        get_validation,
        record_validation,
    )

    p = tmp_path / "b.kicad_pcb"
    p.write_text("v1", encoding="utf-8")
    record_validation(p, "validate_connector_orientations", {"passed": True})

    p.write_text("v2", encoding="utf-8")
    assert get_validation(p, "validate_connector_orientations") is None


def test_record_validation_invalidates_other_validators_on_hash_change(tmp_path: Path):
    """When board hash changes, the entire cache is reset — no stale entries leak through."""
    from kicad_mcp.utils.validation_cache import (
        get_validation,
        record_validation,
    )

    p = tmp_path / "b.kicad_pcb"
    p.write_text("v1", encoding="utf-8")
    record_validation(p, "validate_connector_orientations", {"passed": True})
    record_validation(p, "check_courtyard_overlaps", {"passed": True})
    # Confirm both present at v1
    assert get_validation(p, "validate_connector_orientations") is not None
    assert get_validation(p, "check_courtyard_overlaps") is not None

    # Change board content
    p.write_text("v2", encoding="utf-8")
    # Recording one validator at v2 must NOT carry forward the v1 entries
    record_validation(p, "validate_connector_orientations", {"passed": True})
    assert get_validation(p, "validate_connector_orientations") is not None
    assert get_validation(p, "check_courtyard_overlaps") is None
