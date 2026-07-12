"""Unit tests for the #20/R1 in-process launch-window helper."""

from __future__ import annotations

from unittest.mock import patch

from kicad_mcp.utils import startup_state


def setup_function(_fn) -> None:
    startup_state.reset_launch_state()


def teardown_function(_fn) -> None:
    startup_state.reset_launch_state()


def test_recent_launch_false_before_any_launch():
    assert startup_state.recent_launch() is False


def test_recent_launch_true_right_after_launch():
    startup_state.note_launch()
    assert startup_state.recent_launch() is True


def test_recent_launch_false_outside_window():
    startup_state.note_launch()
    # A zero-length window makes any elapsed time "not recent".
    assert startup_state.recent_launch(window_s=0.0) is False


def test_recent_launch_respects_window_boundary():
    # note_launch stamps t0; move the clock forward and probe both sides.
    with patch("kicad_mcp.utils.startup_state.time.monotonic", return_value=1000.0):
        startup_state.note_launch()
    with patch("kicad_mcp.utils.startup_state.time.monotonic", return_value=1025.0):
        assert startup_state.recent_launch(window_s=30.0) is True
    with patch("kicad_mcp.utils.startup_state.time.monotonic", return_value=1035.0):
        assert startup_state.recent_launch(window_s=30.0) is False


def test_reset_clears_launch():
    startup_state.note_launch()
    assert startup_state.recent_launch() is True
    startup_state.reset_launch_state()
    assert startup_state.recent_launch() is False
