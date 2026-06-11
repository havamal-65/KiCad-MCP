"""REQ-COV-016 — silk-centroid regression caught via the MCP layer.

This is the canonical "logic right against mocks, wrong against real KiCad"
catch (umbrella HLRP §6.1 goal). The bug shipped 2026-05-21: the unit suite
passed with the silk-centroid code path mocked, but on the real
`bt_audio_v1` board the validator missed an inward-facing USB-C connector
because the centroid frame transform was inverted.

Commit f4b99e1 fixed the math. Reverting it MUST cause this test to fail.

Pure-file test against a checked-in fixture board — does not touch the
bridge or require pcbnew open. Lives in the default test suite (not
tests/integration/) so the catch-class fires on every pytest run, not
only when KICAD_INTEGRATION is set.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from kicad_mcp.tools.drc import run_validate_connector_orientations


_FIXTURE_BOARD = Path("tests/fixtures/boards/bt_audio_v1_before_connector_fix.kicad_pcb")


def test_silk_centroid_fix_via_mcp_tool(tmp_path):
    """REQ-COV-016: validator flags J1 on the known-broken bt_audio_v1 board."""
    assert _FIXTURE_BOARD.is_file(), (
        f"Fixture board missing: {_FIXTURE_BOARD}. "
        f"Run from repo root or restore the fixture from git."
    )

    board_copy = tmp_path / _FIXTURE_BOARD.name
    shutil.copy2(_FIXTURE_BOARD, board_copy)

    result = run_validate_connector_orientations(board_copy)

    assert isinstance(result, dict), f"unexpected result type: {result!r}"
    violations = result.get("violations", [])
    refs = [v.get("ref") for v in violations]
    assert "J1" in refs, (
        f"J1 missing from violations — the silk-centroid regression is back. "
        f"Commit f4b99e1 fix appears reverted or broken. "
        f"violations={violations!r}"
    )
