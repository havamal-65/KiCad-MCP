"""F-DV7 (DV build) — run_drc must count kicad-cli's ``unconnected_items``.

kicad-cli's DRC JSON reports missing connections in a separate top-level
``unconnected_items`` array, not under ``violations``. The pre-fix parser read
only ``violations``, so a completely unrouted board (20 error-severity missing
connections on the ecc83 recreation) came back ``passed: true, error_count: 0``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kicad_mcp.backends.cli_backend import CLIDRCOps


def _run_drc_with_report(tmp_path: Path, report: dict) -> dict:
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb (version 20241229) (generator t))", encoding="utf-8")
    output = tmp_path / "drc.json"

    def fake_run(self, args, timeout=60):  # type: ignore[no-untyped-def]
        output.write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch(
        "kicad_mcp.utils.platform_helper.find_kicad_cli",
        return_value=Path("kicad-cli"),
    ), patch("kicad_mcp.backends.cli_backend.CLIDRCOps._run", fake_run):
        return CLIDRCOps(Path("kicad-cli")).run_drc(board, output)


def test_unconnected_items_fail_drc(tmp_path: Path) -> None:
    report = {
        "violations": [],
        "unconnected_items": [
            {
                "severity": "error",
                "type": "unconnected_items",
                "description": "Missing connection between items",
                "items": [],
            },
            {
                "severity": "error",
                "type": "unconnected_items",
                "description": "Missing connection between items",
                "items": [],
            },
        ],
    }
    result = _run_drc_with_report(tmp_path, report)
    assert result["passed"] is False
    assert result["error_count"] == 2
    assert result["unconnected_count"] == 2
    assert [v["type"] for v in result["violations"]] == [
        "unconnected_items", "unconnected_items",
    ]


def test_routed_board_with_warnings_still_passes(tmp_path: Path) -> None:
    report = {
        "violations": [
            {"severity": "warning", "type": "silk_overlap",
             "description": "w", "items": []},
        ],
        "unconnected_items": [],
    }
    result = _run_drc_with_report(tmp_path, report)
    assert result["passed"] is True
    assert result["error_count"] == 0
    assert result["warning_count"] == 1
    assert result["unconnected_count"] == 0
