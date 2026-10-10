"""Unit tests for the U5 KiCad version switcher (VS-1..VS-5).

Covers `processes.running_pcbnew_versions` parsing and the LauncherApi
version methods with the resolver's install enumeration mocked — no KiCad
installation or GUI needed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# launcher.app imports webview at module level; without the [launcher] extra
# installed, collection of this module must skip rather than error.
pytest.importorskip("webview")

from kicad_mcp.utils import kicad_version as kv
from launcher import processes, settings
from launcher.app import LauncherApi
from launcher.config import LauncherConfig


def _cfg(tmp_path: Path) -> LauncherConfig:
    return LauncherConfig(
        venv_python=tmp_path / "python.exe",
        venv_pythonw=tmp_path / "pythonw.exe",
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_config_path=tmp_path / ".mcp.dev.json",
        projects_roots=[],
        recents_path=tmp_path / "store" / "recents.json",
    )


@pytest.fixture(autouse=True)
def _clean_resolver():
    """Isolate resolver module state (pin + env) across tests."""
    kv._preferred_override = None
    os.environ.pop(kv.ENV_PREFERRED, None)
    yield
    kv._preferred_override = None
    os.environ.pop(kv.ENV_PREFERRED, None)


def _installs(*versions: str) -> list[kv.KicadInstall]:
    return [kv.KicadInstall(v, Path(f"C:/kicad/{v}/bin"), None, None) for v in versions]


# --- running_pcbnew_versions (VS-4 input) ------------------------------------

class _Proc:
    def __init__(self, name, exe):
        self.info = {"name": name, "exe": exe}


def test_running_pcbnew_versions_parse(monkeypatch):
    psutil = pytest.importorskip("psutil")
    procs = [
        _Proc("pcbnew.exe", r"C:\Program Files\KiCad\10.0\bin\pcbnew.exe"),
        _Proc("pcbnew.exe", r"C:\Program Files\KiCad\9.0\bin\pcbnew.exe"),
        _Proc("pcbnew.exe", r"C:\Program Files\KiCad\10.0\bin\pcbnew.exe"),  # dedup
        _Proc("kicad.exe", r"C:\Program Files\KiCad\10.0\bin\kicad.exe"),    # not pcbnew
        _Proc("pcbnew.exe", r"D:\odd\place\pcbnew.exe"),                     # no version dir
        _Proc("pcbnew.exe", None),                                           # exe unreadable
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    assert processes.running_pcbnew_versions() == ["10.0", "9.0"]


def test_running_pcbnew_versions_forward_slashes(monkeypatch):
    psutil = pytest.importorskip("psutil")
    procs = [_Proc("pcbnew.exe", "/opt/KiCad/9.0/bin/pcbnew.exe")]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    assert processes.running_pcbnew_versions() == ["9.0"]


# --- LauncherApi version methods (VS-2/VS-3/VS-4) ----------------------------

def _api(tmp_path, monkeypatch, installs):
    monkeypatch.setattr(kv, "installed", lambda refresh=False: installs)
    monkeypatch.setattr(
        "kicad_mcp.utils.platform_helper.find_pcbnew_executable", lambda: None
    )
    monkeypatch.setattr(processes, "running_pcbnew_versions", lambda: [])
    api = LauncherApi.__new__(LauncherApi)
    api.cfg = _cfg(tmp_path)
    api.settings = settings.load_settings(api.cfg)
    api._projects = []
    api._selected = 0
    api._pcbnew_exe = None
    api._setup_cache = object()  # must be invalidated by a switch
    return api


def test_set_kicad_version_pins_env_and_persists(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0", "9.0"))
    res = api.set_kicad_version("9.0")
    assert res["ok"] is True and res["version"] == "9.0"
    assert os.environ[kv.ENV_PREFERRED] == "9.0"
    assert kv.preferred().version == "9.0"
    assert settings.load_settings(api.cfg)["kicad_version"] == "9.0"
    assert api._setup_cache is None


def test_set_kicad_version_rejects_uninstalled(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0"))
    res = api.set_kicad_version("8.0")
    assert res["ok"] is False
    assert any("not installed" in m for m in res["messages"])
    assert kv.ENV_PREFERRED not in os.environ
    assert settings.load_settings(api.cfg)["kicad_version"] is None


def test_set_kicad_version_empty_clears_pin(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0", "9.0"))
    api.set_kicad_version("9.0")
    res = api.set_kicad_version("")
    assert res["ok"] is True and res["version"] == "10.0"   # newest default
    assert kv.ENV_PREFERRED not in os.environ
    assert settings.load_settings(api.cfg)["kicad_version"] is None


def test_boot_pin_clears_uninstalled_persisted_version(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0"))
    settings.save_settings(api.cfg, kicad_version="9.0")
    api.settings = settings.load_settings(api.cfg)
    api._apply_version_pin(api.settings.get("kicad_version"))
    assert kv.preferred().version == "10.0"
    assert kv.ENV_PREFERRED not in os.environ
    assert settings.load_settings(api.cfg)["kicad_version"] is None  # self-healed


def test_version_notes_port_collision(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0", "9.0"))
    api.set_kicad_version("10.0")
    monkeypatch.setattr(processes, "running_pcbnew_versions", lambda: ["9.0"])
    notes = api._version_notes()
    assert len(notes) == 1
    assert "9.0" in notes[0] and "10.0" in notes[0]


def test_version_notes_quiet_when_selected_is_running(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0", "9.0"))
    api.set_kicad_version("10.0")
    monkeypatch.setattr(processes, "running_pcbnew_versions", lambda: ["10.0"])
    assert api._version_notes() == []


def test_set_auto_surface_persists(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch, _installs("10.0"))
    res = api.set_auto_surface(False)
    assert res["ok"] is True and res["autoSurface"] is False
    assert settings.load_settings(api.cfg)["auto_surface"] is False
    api.set_auto_surface(True)
    assert settings.load_settings(api.cfg)["auto_surface"] is True
