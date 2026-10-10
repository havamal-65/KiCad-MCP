"""Unit tests for kicad_mcp.utils.launcher_surface (U5 AS-1/AS-2/AS-4).

The spawn itself is always mocked; the only real-OS tests are the named-mutex
pair, which run on Windows only (the module is a no-op elsewhere by design).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kicad_mcp.utils import launcher_surface as ls


# --- feature gate (auto_surface_enabled) -------------------------------------

def _settings_file(tmp_path, monkeypatch, text):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv(ls.ENV_DISABLE, raising=False)
    p = tmp_path / "kicad-mcp-launcher" / "settings.json"
    if text is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return p


def test_enabled_by_default_when_no_settings(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, None)
    assert ls.auto_surface_enabled() is True


def test_enabled_tolerates_corrupt_settings(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, "{nope")
    assert ls.auto_surface_enabled() is True


def test_settings_false_disables(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, '{"auto_surface": false}')
    assert ls.auto_surface_enabled() is False


def test_settings_non_bool_means_default_on(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, '{"auto_surface": "no"}')
    assert ls.auto_surface_enabled() is True


def test_env_kill_switch_beats_settings(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, '{"auto_surface": true}')
    monkeypatch.setenv(ls.ENV_DISABLE, "1")
    assert ls.auto_surface_enabled() is False


def test_env_zero_or_empty_does_not_disable(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, '{"auto_surface": true}')
    for v in ("", "0", " "):
        monkeypatch.setenv(ls.ENV_DISABLE, v)
        assert ls.auto_surface_enabled() is True


# --- spawn_launcher (dev path; spawn mocked) ---------------------------------

def _dev_tree(tmp_path, with_pythonw=True):
    (tmp_path / "launcher").mkdir()
    (tmp_path / "launcher" / "__main__.py").write_text("", encoding="utf-8")
    pyw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    if with_pythonw:
        pyw.parent.mkdir(parents=True)
        pyw.write_text("", encoding="utf-8")
    return pyw


def test_spawn_dev_path_uses_venv_pythonw(tmp_path, monkeypatch):
    pyw = _dev_tree(tmp_path)
    monkeypatch.setattr(ls, "_repo_root", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(ls, "_spawn_detached", lambda cmd, cwd: calls.append((cmd, cwd)) or True)
    assert ls.spawn_launcher() is True
    assert calls == [([str(pyw), "-m", "launcher"], tmp_path)]


def test_spawn_false_without_entry_point(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "_repo_root", lambda: tmp_path)  # no launcher/ at all
    called = []
    monkeypatch.setattr(ls, "_spawn_detached", lambda cmd, cwd: called.append(cmd) or True)
    assert ls.spawn_launcher() is False
    assert called == []


def test_spawn_false_without_venv_pythonw(tmp_path, monkeypatch):
    _dev_tree(tmp_path, with_pythonw=False)
    monkeypatch.setattr(ls, "_repo_root", lambda: tmp_path)
    called = []
    monkeypatch.setattr(ls, "_spawn_detached", lambda cmd, cwd: called.append(cmd) or True)
    assert ls.spawn_launcher() is False
    assert called == []


# --- maybe_surface_launcher outcome ladder -----------------------------------

def test_maybe_off_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ls.maybe_surface_launcher() == "off_platform"


def _win(monkeypatch, enabled=True, running=False, spawn=True):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ls, "auto_surface_enabled", lambda: enabled)
    monkeypatch.setattr(ls, "launcher_running", lambda: running)
    monkeypatch.setattr(ls, "spawn_launcher", lambda: spawn)


def test_maybe_disabled(monkeypatch):
    _win(monkeypatch, enabled=False)
    assert ls.maybe_surface_launcher() == "disabled"


def test_maybe_already_running(monkeypatch):
    _win(monkeypatch, running=True)
    assert ls.maybe_surface_launcher() == "already_running"


def test_maybe_spawned(monkeypatch):
    _win(monkeypatch)
    assert ls.maybe_surface_launcher() == "spawned"


def test_maybe_unavailable_on_spawn_failure(monkeypatch):
    _win(monkeypatch, spawn=False)
    assert ls.maybe_surface_launcher() == "unavailable"


def test_maybe_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(ls, "auto_surface_enabled", boom)
    assert ls.maybe_surface_launcher() == "unavailable"


# --- named mutex (real OS, Windows only) -------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_mutex_singleton_roundtrip():
    import ctypes

    handle = ls.acquire_launcher_mutex()
    if handle is None:
        # A real launcher window holds the mutex on this machine — the refusal
        # itself is the singleton behavior under test.
        assert ls.launcher_running() is True
        return
    try:
        assert ls.launcher_running() is True
        assert ls.acquire_launcher_mutex() is None  # second launcher refused
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform == "win32", reason="off-Windows no-op leg")
def test_mutex_noop_off_windows():
    assert ls.acquire_launcher_mutex() is None
    assert ls.launcher_running() is False


def test_settings_path_shares_launcher_convention(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert ls._settings_path() == Path(tmp_path) / "kicad-mcp-launcher" / "settings.json"
