"""Tests for subprocess_backend helper utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.backends.subprocess_backend import (
    _BOARD_LOAD_FAILED_SENTINEL,
    _format_pcbnew_error,
    _malformed_board_message,
    _normalize_error_text,
)


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
