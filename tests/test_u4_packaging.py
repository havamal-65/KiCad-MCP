"""U4 units — frozen-mode wiring for the packaged app (REQ-U4-003/006).

All V-A: the frozen marker, the sibling-exe server spawn (dev argv
byte-identical to pre-U4), server-process recognition for both cmdline
shapes, the bundled bridge source path, the in-process frozen bridge
installer, the tkinter-free monitor import, and the WebView2 probe.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from launcher import config as config_mod
from launcher import processes
from launcher import setup_core
from launcher.config import LauncherConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = REPO_ROOT / "scripts" / "mcp_health_monitor.py"


def _cfg(tmp_path: Path) -> LauncherConfig:
    return LauncherConfig(
        venv_python=tmp_path / "venv" / "python.exe",
        venv_pythonw=tmp_path / "venv" / "pythonw.exe",
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_config_path=tmp_path / ".mcp.dev.json",
        projects_roots=[],
        recents_path=tmp_path / "recents.json",
    )


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)


# ── the marker (REQ-U4-003: explicit, not heuristic) ─────────────────────────

def test_is_frozen_false_in_dev():
    assert config_mod.is_frozen() is False


def test_is_frozen_true_under_pyinstaller_marker(frozen: None):
    assert config_mod.is_frozen() is True


def test_frozen_server_exe_is_sibling_of_executable():
    exe = config_mod.frozen_server_exe()
    assert exe.parent == Path(sys.executable).parent
    assert exe.name.startswith("kicad-mcp-server")


# ── server spawn (dev argv byte-identical; frozen -> sibling exe) ────────────

def _spawn_argv(cfg: LauncherConfig) -> tuple[list[str], str]:
    with patch.object(processes, "mcp_http_running", return_value="down"), \
         patch.object(processes.subprocess, "Popen") as popen:
        result = processes.start_mcp_http(cfg)
    assert result.action == "started", result
    args, kwargs = popen.call_args
    return args[0], kwargs["cwd"]


def test_start_mcp_http_dev_argv_unchanged(tmp_path: Path):
    cfg = _cfg(tmp_path)
    argv, cwd = _spawn_argv(cfg)
    assert argv == [
        str(cfg.venv_python), "-m", "kicad_mcp_plugin",
        "--transport", "streamable-http",
        "--host", "127.0.0.1", "--port", "8765",
    ]
    assert cwd == str(processes.REPO_ROOT)


def test_start_mcp_http_frozen_uses_sibling_exe(tmp_path: Path, frozen: None):
    cfg = _cfg(tmp_path)
    argv, cwd = _spawn_argv(cfg)
    assert argv[0] == str(config_mod.frozen_server_exe())
    assert "kicad_mcp_plugin" not in argv  # no `-m` module spawn when frozen
    assert argv[1:] == [
        "--transport", "streamable-http",
        "--host", "127.0.0.1", "--port", "8765",
    ]
    assert cwd == str(Path(sys.executable).parent)


# ── server recognition (both cmdline shapes, still port-aware) ───────────────

def test_matches_server_dev_cmdline():
    cmd = ["python.exe", "-m", "kicad_mcp_plugin",
           "--transport", "streamable-http", "--port", "8765"]
    assert processes._matches_server(cmd, 8765) is True
    assert processes._matches_server(cmd, 9000) is False


def test_matches_server_frozen_cmdline():
    cmd = [r"C:\Apps\KiCad-MCP Launcher\kicad-mcp-server.exe",
           "--transport", "streamable-http", "--port", "8765"]
    assert processes._matches_server(cmd, 8765) is True
    assert processes._matches_server(cmd, 9000) is False


def test_matches_server_rejects_foreign():
    assert processes._matches_server(
        ["python.exe", "-m", "http.server", "--port", "8765"], 8765) is False


# ── bridge source + frozen installer ─────────────────────────────────────────

def test_bridge_source_path_dev():
    assert setup_core.bridge_source_path() == (
        REPO_ROOT / "kicad_plugin" / "kicad_mcp_bridge.py")


def test_bridge_source_path_frozen(frozen: None):
    import kicad_mcp_plugin

    expected = (Path(kicad_mcp_plugin.__file__).resolve().parent
                / "data" / "kicad_mcp_bridge.py")
    assert setup_core.bridge_source_path() == expected


def test_reinstall_bridge_frozen_runs_in_process(frozen: None):
    calls: list[str] = []
    with patch("kicad_mcp_plugin.install_bridge._all_versions",
               return_value=["10.0", "9.0"]), \
         patch("kicad_mcp_plugin.install_bridge.install_bridge",
               side_effect=lambda version: calls.append(version) or 0), \
         patch.object(processes.subprocess, "run") as run:
        result = processes.reinstall_bridge()
    assert result.action == "started", result
    assert calls == ["10.0", "9.0"]
    run.assert_not_called()  # never pwsh in the packaged app


def test_reinstall_bridge_frozen_reports_failures(frozen: None):
    with patch("kicad_mcp_plugin.install_bridge._all_versions",
               return_value=["10.0", "9.0"]), \
         patch("kicad_mcp_plugin.install_bridge.install_bridge",
               side_effect=lambda version: 1 if version == "9.0" else 0):
        result = processes.reinstall_bridge()
    assert result.action == "failed"
    assert "9.0" in (result.reason or "")


def test_reinstall_bridge_frozen_no_kicad(frozen: None):
    with patch("kicad_mcp_plugin.install_bridge._all_versions", return_value=[]):
        result = processes.reinstall_bridge()
    assert result.action == "failed"
    assert "no KiCad" in (result.reason or "")


# ── monitor imports without tkinter (packaged app has no tcl/tk) ─────────────

def test_monitor_imports_without_tkinter(monkeypatch: pytest.MonkeyPatch):
    for name in [m for m in sys.modules if m == "tkinter" or m.startswith("tkinter.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "tkinter", None)  # import -> ImportError
    spec = importlib.util.spec_from_file_location("_u4_monitor_probe", MONITOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not raise
    assert mod.tk is None
    assert hasattr(mod, "Poller")  # the piece the launcher actually uses


# ── WebView2 probe ───────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry probe")
def test_webview2_probe_absent_and_present():
    from launcher import app as app_mod

    with patch("winreg.OpenKey", side_effect=OSError):
        assert app_mod._webview2_present() is False

    handle = MagicMock()
    handle.__enter__ = MagicMock(return_value=handle)
    handle.__exit__ = MagicMock(return_value=False)
    with patch("winreg.OpenKey", return_value=handle), \
         patch("winreg.QueryValueEx", return_value=("124.0.2478.67", 1)):
        assert app_mod._webview2_present() is True
    # the documented "0.0.0.0" tombstone counts as absent
    with patch("winreg.OpenKey", return_value=handle), \
         patch("winreg.QueryValueEx", return_value=("0.0.0.0", 1)):
        assert app_mod._webview2_present() is False
