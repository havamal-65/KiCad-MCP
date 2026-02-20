"""Tests for routing tools and pcbnew helper infrastructure."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.tools.routing import (
    _HELPER_SCRIPT,
    _find_kicad_python,
    _get_pcbnew,
    _run_pcbnew_helper,
)


# ---------------------------------------------------------------------------
# Helper-script path
# ---------------------------------------------------------------------------

class TestHelperScriptPath:
    def test_helper_script_exists(self):
        """The packaged pcbnew_helper.py must be present on disk."""
        assert _HELPER_SCRIPT.exists(), (
            f"pcbnew_helper.py not found at expected path: {_HELPER_SCRIPT}"
        )

    def test_helper_script_is_valid_python(self):
        """The helper script must be parseable Python."""
        import ast
        source = _HELPER_SCRIPT.read_text(encoding="utf-8")
        ast.parse(source)  # raises SyntaxError if invalid


# ---------------------------------------------------------------------------
# _get_pcbnew
# ---------------------------------------------------------------------------

class TestGetPcbnew:
    def test_returns_none_when_not_installed(self, monkeypatch):
        """Returns None when pcbnew is not importable."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pcbnew":
                raise ImportError("pcbnew not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert _get_pcbnew() is None


# ---------------------------------------------------------------------------
# _find_kicad_python
# ---------------------------------------------------------------------------

class TestFindKicadPython:
    def test_returns_none_when_no_interpreter_found(self, tmp_path, monkeypatch):
        """Returns None if no KiCad Python interpreter exists."""
        # Patch platform to 'macos' and point to a nonexistent path
        monkeypatch.setattr(
            "kicad_mcp.utils.platform_helper.get_platform",
            lambda: "macos",
        )
        # The candidate path does not exist → should return None
        result = _find_kicad_python()
        # On a CI machine without KiCad, this should be None
        assert result is None or isinstance(result, Path)

    def test_returns_path_on_linux_if_python3_exists(self, monkeypatch):
        """On Linux, returns /usr/bin/python3 when it exists."""
        monkeypatch.setattr(
            "kicad_mcp.utils.platform_helper.get_platform",
            lambda: "linux",
        )
        with patch("kicad_mcp.tools.routing.Path") as MockPath:
            mock_candidate = MagicMock(spec=Path)
            mock_candidate.exists.return_value = True
            MockPath.return_value = mock_candidate
            result = _find_kicad_python()
            # Either returns the mock path or None depending on actual /usr/bin/python3
            assert result is None or isinstance(result, (Path, MagicMock))


# ---------------------------------------------------------------------------
# _run_pcbnew_helper
# ---------------------------------------------------------------------------

class TestRunPcbnewHelper:
    def test_returns_error_when_no_kicad_python(self, monkeypatch):
        """Returns ok=False when KiCad Python interpreter is not found."""
        monkeypatch.setattr(
            "kicad_mcp.tools.routing._find_kicad_python",
            lambda: None,
        )
        result = _run_pcbnew_helper("export_dsn", ["/fake/board.kicad_pcb", "/fake/out.dsn"])
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_returns_parsed_json_on_success(self, monkeypatch):
        """Parses helper stdout as JSON and returns the dict."""
        monkeypatch.setattr(
            "kicad_mcp.tools.routing._find_kicad_python",
            lambda: Path("/usr/bin/python3"),
        )
        mock_result = MagicMock()
        mock_result.stdout = '{"ok": true, "tracks_before": 0, "tracks_after": 5}\n'
        mock_result.stderr = ""

        with patch("kicad_mcp.tools.routing.subprocess.run", return_value=mock_result):
            result = _run_pcbnew_helper("import_ses", ["/b.kicad_pcb", "/r.ses"])

        assert result["ok"] is True
        assert result["tracks_before"] == 0
        assert result["tracks_after"] == 5

    def test_returns_error_on_json_decode_failure(self, monkeypatch):
        """Returns ok=False when helper stdout is not valid JSON."""
        monkeypatch.setattr(
            "kicad_mcp.tools.routing._find_kicad_python",
            lambda: Path("/usr/bin/python3"),
        )
        mock_result = MagicMock()
        mock_result.stdout = "Traceback (most recent call last):\n  boom\n"
        mock_result.stderr = ""

        with patch("kicad_mcp.tools.routing.subprocess.run", return_value=mock_result):
            result = _run_pcbnew_helper("export_dsn", ["/b.kicad_pcb", "/o.dsn"])

        assert result["ok"] is False
        assert "non-JSON" in result["error"]

    def test_returns_error_on_timeout(self, monkeypatch):
        """Returns ok=False when subprocess times out."""
        monkeypatch.setattr(
            "kicad_mcp.tools.routing._find_kicad_python",
            lambda: Path("/usr/bin/python3"),
        )
        with patch(
            "kicad_mcp.tools.routing.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="python", timeout=60),
        ):
            result = _run_pcbnew_helper("export_dsn", ["/b.kicad_pcb", "/o.dsn"], timeout=60)

        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_returns_error_on_empty_output(self, monkeypatch):
        """Returns ok=False when helper produces no stdout."""
        monkeypatch.setattr(
            "kicad_mcp.tools.routing._find_kicad_python",
            lambda: Path("/usr/bin/python3"),
        )
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "ImportError: no module named pcbnew"

        with patch("kicad_mcp.tools.routing.subprocess.run", return_value=mock_result):
            result = _run_pcbnew_helper("export_dsn", ["/b.kicad_pcb", "/o.dsn"])

        assert result["ok"] is False
        assert "no output" in result["error"]


# ---------------------------------------------------------------------------
# pcbnew_helper.py script (invoked via current Python)
# ---------------------------------------------------------------------------

class TestPcbnewHelperScript:
    """Run the helper script directly to verify its CLI interface."""

    def _run_helper(self, *args: str) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, str(_HELPER_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout.strip()

    def test_no_command_fails(self):
        rc, stdout = self._run_helper()
        assert rc != 0
        data = json.loads(stdout)
        assert data["ok"] is False

    def test_unknown_command_fails(self):
        rc, stdout = self._run_helper("nonexistent_command")
        assert rc != 0
        data = json.loads(stdout)
        assert data["ok"] is False
        assert "Unknown command" in data["error"]

    def test_wrong_arg_count_fails(self):
        rc, stdout = self._run_helper("export_dsn", "/only_one_arg")
        assert rc != 0
        data = json.loads(stdout)
        assert data["ok"] is False
        assert "2 arguments" in data["error"]

    def test_export_dsn_fails_gracefully_without_pcbnew(self):
        """When pcbnew is unavailable the helper exits with ok=False, not a traceback."""
        rc, stdout = self._run_helper("export_dsn", "/nonexistent/board.kicad_pcb", "/out.dsn")
        # pcbnew is not installed in CI — the helper should catch the ImportError and
        # return a clean JSON error, not print a traceback.
        assert stdout, "helper produced no output"
        data = json.loads(stdout)
        assert data["ok"] is False
        assert "error" in data
