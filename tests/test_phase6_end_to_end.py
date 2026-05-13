"""End-to-end Phase 6 integration test.

Verifies §6.4 criterion #1: re-running the Phase 6 flow on a board with
inward-facing connectors produces outward-facing connectors.

Pipeline exercised:
    identify_edge_facing_connectors
        → validate_connector_orientations (expects FAIL with violations)
            → place_at_edge for each violation
                → validate_connector_orientations (expects PASS)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.file_backend import FileBoardOps


class _FileBackend(BackendProtocol):
    def __init__(self):
        self._ops = FileBoardOps()

    def get_board_ops(self):
        return self._ops

    def get_board_modify_ops(self):
        return self._ops


REGRESSION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "boards"
    / "bt_audio_v1_before_connector_fix.kicad_pcb"
)


def _call_place_at_edge(board_path: Path, reference: str, edge: str) -> dict:
    """Invoke the place_at_edge MCP tool against a real FileBoardOps."""
    import fastmcp
    from kicad_mcp.tools import board
    from kicad_mcp.utils.change_log import ChangeLog

    backend = _FileBackend()
    change_log = ChangeLog(board_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    board.register_tools(mcp, backend, change_log)
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "place_at_edge")
    return json.loads(tool_fn(str(board_path), reference=reference, edge=edge))


@pytest.mark.skipif(not REGRESSION_FIXTURE.exists(), reason="regression fixture not present")
def test_phase6_repairs_broken_board(tmp_path: Path):
    """The full Phase 6 stack can repair a known-broken board.

    bt_audio_v1.before_connector_fix.kicad_pcb has J2 (and possibly others)
    facing inward. After running place_at_edge on each violation, the board
    must pass validate_connector_orientations.
    """
    from kicad_mcp.tools.drc import (
        run_identify_edge_facing_connectors,
        run_validate_connector_orientations,
    )

    scratch = tmp_path / "bt_audio_v1.kicad_pcb"
    shutil.copyfile(REGRESSION_FIXTURE, scratch)

    # ── Step 1: identify edge-facing connectors ──────────────────────────────
    identify = run_identify_edge_facing_connectors(scratch)
    refs = {c["ref"] for c in identify["connectors"]}
    assert {"J1", "J2", "J3"}.issubset(refs), (
        f"Expected J1/J2/J3 detected as edge-facing, got {refs}"
    )

    # ── Step 2: initial validation must fail ─────────────────────────────────
    initial = run_validate_connector_orientations(scratch)
    assert not initial["passed"], (
        "Pre-fix bt_audio_v1 should fail validation; if it now passes, "
        "either the fixture was regenerated or the validator regressed."
    )
    initial_violation_count = len(initial["violations"])
    assert initial_violation_count > 0

    # ── Step 3: place_at_edge for each violation ─────────────────────────────
    for v in initial["violations"]:
        ref = v["ref"]
        edge = v["suggested_edge"]
        result = _call_place_at_edge(scratch, ref, edge)
        assert result["status"] == "success", (
            f"place_at_edge failed for {ref} at {edge}: {result.get('message')}"
        )

    # ── Step 4: re-validate must pass ───────────────────────────────────────
    final = run_validate_connector_orientations(scratch)
    assert final["passed"], (
        f"Board still fails after Phase 6 remediation. "
        f"initial violations: {initial_violation_count}, "
        f"final violations: {final['violations']}"
    )

    # ── Step 5: autoroute gate now accepts the board ─────────────────────────
    from kicad_mcp.utils.validation_cache import get_validation
    cached = get_validation(scratch, "validate_connector_orientations")
    assert cached is not None and cached["passed"], (
        "Validation cache should record the passing result so autoroute proceeds."
    )
