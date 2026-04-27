"""Tests for SubprocessBackend — availability check and error paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.subprocess_backend import (
    SubprocessBackend,
    SubprocessBoardOps,
    _BOARD_LOAD_FAILED_SENTINEL,
    _format_pcbnew_error,
    _malformed_board_message,
    _normalize_error_text,
)


# ---------------------------------------------------------------------------
# SubprocessBackend.is_available()
# ---------------------------------------------------------------------------

def test_is_available_returns_false_when_no_kicad_python():
    with patch("kicad_mcp.backends.subprocess_backend._get_kicad_python", return_value=None):
        backend = SubprocessBackend()
        assert backend.is_available() is False


def test_is_available_returns_true_when_kicad_python_exists(tmp_path: Path):
    fake_python = tmp_path / "python.exe"
    fake_python.touch()
    with patch("kicad_mcp.backends.subprocess_backend._get_kicad_python", return_value=fake_python):
        backend = SubprocessBackend()
        assert backend.is_available() is True


def test_backend_name():
    backend = SubprocessBackend()
    assert backend.name == "pcbnew_subprocess"


def test_backend_capabilities():
    from kicad_mcp.backends.base import BackendCapability
    backend = SubprocessBackend()
    assert BackendCapability.BOARD_ROUTE in backend.capabilities
    assert len(backend.capabilities) == 1


# ---------------------------------------------------------------------------
# SubprocessBoardOps.export_dsn — failure paths via mocked _run_pcbnew_script
# ---------------------------------------------------------------------------

def test_export_dsn_raises_on_board_load_failure(tmp_path: Path):
    board = tmp_path / "test.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    dsn = tmp_path / "test.dsn"

    failure_output = f"Error: {_BOARD_LOAD_FAILED_SENTINEL} could not read board"
    with patch("kicad_mcp.backends.subprocess_backend._run_pcbnew_script",
               return_value=(False, failure_output)):
        ops = SubprocessBoardOps()
        with pytest.raises(RuntimeError, match="[Mm]alformed board|[Cc]ould not load"):
            ops.export_dsn(board, dsn)


def test_export_dsn_raises_on_script_failure(tmp_path: Path):
    board = tmp_path / "test.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    dsn = tmp_path / "test.dsn"

    with patch("kicad_mcp.backends.subprocess_backend._run_pcbnew_script",
               return_value=(False, "some error output")):
        ops = SubprocessBoardOps()
        with pytest.raises(RuntimeError):
            ops.export_dsn(board, dsn)


# ---------------------------------------------------------------------------
# Pure helper function tests (no subprocess needed)
# ---------------------------------------------------------------------------

def test_normalize_error_text_collapses_whitespace():
    result = _normalize_error_text("  line1\n  line2\n  line3  ")
    assert "\n" not in result
    assert "line1" in result
    assert "line2" in result


def test_normalize_error_text_truncates_long_output():
    long_text = "x" * 2000
    result = _normalize_error_text(long_text, max_chars=100)
    assert len(result) <= 103  # 100 chars + "..."
    assert result.endswith("...")


def test_normalize_error_text_short_string_unchanged():
    result = _normalize_error_text("short error")
    assert result == "short error"


def test_malformed_board_message_includes_path(tmp_path: Path):
    board = tmp_path / "my_board.kicad_pcb"
    msg = _malformed_board_message(board, "details here")
    assert "my_board.kicad_pcb" in msg
    assert "details here" in msg


def test_format_pcbnew_error_with_load_sentinel(tmp_path: Path):
    board = tmp_path / "board.kicad_pcb"
    output = f"Something went wrong {_BOARD_LOAD_FAILED_SENTINEL} extra"
    msg = _format_pcbnew_error("DSN export failed", output, board_path=board)
    # Load sentinel triggers the malformed-board message
    assert "board.kicad_pcb" in msg


def test_format_pcbnew_error_without_sentinel():
    msg = _format_pcbnew_error("export failed", "permission denied")
    assert "export failed" in msg
    assert "permission denied" in msg
