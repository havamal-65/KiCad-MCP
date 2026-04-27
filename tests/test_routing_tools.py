"""Tests for routing tool implementation helpers.

These tests exercise the pure-Python logic paths of _impl_run_freerouter and
_impl_clean_board_for_routing without launching a real FreeRouting subprocess
or importing pcbnew.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.tools.routing import _impl_run_freerouter, _impl_clean_board_for_routing
from kicad_mcp.utils.change_log import ChangeLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs):
    """Create a minimal KiCadMCPConfig-like mock."""
    cfg = MagicMock()
    cfg.java_path = kwargs.get("java_path", None)
    cfg.freerouting_jar = kwargs.get("freerouting_jar", None)
    return cfg


def _make_change_log(tmp_path: Path) -> ChangeLog:
    return ChangeLog(tmp_path / "changes.json")


# ---------------------------------------------------------------------------
# _impl_run_freerouter — early-exit error paths (no subprocess needed)
# ---------------------------------------------------------------------------

def test_freerouter_returns_error_when_dsn_missing(tmp_path: Path):
    dsn = tmp_path / "board.dsn"  # does NOT exist
    result = json.loads(_impl_run_freerouter(
        dsn_path=str(dsn),
        output="",
        max_passes=3,
        freerouting_jar="",
        java_path="",
        config=_make_config(),
        change_log=_make_change_log(tmp_path),
    ))
    assert result["status"] == "error"
    assert "DSN" in result["message"] or "not found" in result["message"].lower()


def test_freerouter_returns_error_when_java_not_found(tmp_path: Path):
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(PCB)", encoding="utf-8")

    with patch("kicad_mcp.tools.routing.find_java", return_value=None):
        result = json.loads(_impl_run_freerouter(
            dsn_path=str(dsn),
            output="",
            max_passes=3,
            freerouting_jar="",
            java_path="",
            config=_make_config(),
            change_log=_make_change_log(tmp_path),
        ))
    assert result["status"] == "error"
    assert "java" in result["message"].lower() or "Java" in result["message"]


def test_freerouter_returns_error_when_jar_not_found_and_download_fails(tmp_path: Path):
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(PCB)", encoding="utf-8")
    fake_java = tmp_path / "java.exe"
    fake_java.touch()

    with patch("kicad_mcp.tools.routing.find_java", return_value=fake_java), \
         patch("kicad_mcp.tools.routing.find_freerouting_jar", return_value=None), \
         patch("kicad_mcp.tools.routing.download_freerouting", return_value=None):
        result = json.loads(_impl_run_freerouter(
            dsn_path=str(dsn),
            output="",
            max_passes=3,
            freerouting_jar="",
            java_path="",
            config=_make_config(),
            change_log=_make_change_log(tmp_path),
        ))
    assert result["status"] == "error"
    assert "FreeRouting" in result["message"] or "freerouting" in result["message"].lower()


# ---------------------------------------------------------------------------
# _impl_clean_board_for_routing — error paths
# ---------------------------------------------------------------------------

def test_clean_board_raises_when_file_missing(tmp_path: Path):
    """validate_kicad_path raises InvalidPathError when the .kicad_pcb file doesn't exist."""
    from kicad_mcp.models.errors import InvalidPathError
    missing = tmp_path / "ghost.kicad_pcb"
    with pytest.raises(InvalidPathError):
        _impl_clean_board_for_routing(
            path=str(missing),
            remove_keepouts=False,
            remove_unassigned_tracks=False,
            change_log=_make_change_log(tmp_path),
        )


def test_clean_board_returns_error_on_subprocess_failure(tmp_path: Path, tmp_board: Path):
    """When pcbnew is not importable and subprocess script fails, return error JSON."""
    failure_output = "ImportError: No module named 'pcbnew'"
    with patch("kicad_mcp.tools.routing._get_pcbnew", return_value=None), \
         patch("kicad_mcp.tools.routing._run_pcbnew_script", return_value=(False, failure_output)):
        result = json.loads(_impl_clean_board_for_routing(
            path=str(tmp_board),
            remove_keepouts=False,
            remove_unassigned_tracks=False,
            change_log=_make_change_log(tmp_path),
        ))
    assert result["status"] == "error"
    assert "message" in result


def test_clean_board_succeeds_via_subprocess_mock(tmp_path: Path, tmp_board: Path):
    """When subprocess reports success, return success JSON."""
    success_output = "KEEPOUTS=0\nTRACKS=0\n"
    with patch("kicad_mcp.tools.routing._get_pcbnew", return_value=None), \
         patch("kicad_mcp.tools.routing._run_pcbnew_script", return_value=(True, success_output)):
        result = json.loads(_impl_clean_board_for_routing(
            path=str(tmp_board),
            remove_keepouts=False,
            remove_unassigned_tracks=False,
            change_log=_make_change_log(tmp_path),
        ))
    assert result["status"] == "success"
    assert result["keepouts_removed"] == 0
    assert result["tracks_removed"] == 0
