"""Unit tests for the health-UI auto-start wire (Stack Launcher M1 / REQ-TEST-003).

Verifies maybe_launch_health_ui() is env-gated, singleton-guarded, and
best-effort — all at the process-spawn boundary, so no real window is created.
"""

from __future__ import annotations

import pytest

from kicad_mcp.utils import health_ui


@pytest.fixture
def spawned(monkeypatch):
    """Record subprocess.Popen calls and never actually spawn."""
    calls: list[tuple] = []

    def _fake_popen(args, *a, **kw):
        calls.append((args, a, kw))
        return object()

    monkeypatch.setattr(health_ui.subprocess, "Popen", _fake_popen)
    # Pretend the script exists so we exercise the launch path, not the guard.
    monkeypatch.setattr(health_ui.Path, "exists", lambda self: True)
    return calls


def test_no_op_when_env_unset(monkeypatch, spawned):
    monkeypatch.delenv("KICAD_MCP_HEALTH_UI", raising=False)
    health_ui.maybe_launch_health_ui()
    assert spawned == []


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "", "  "])
def test_no_op_when_env_falsy(monkeypatch, spawned, falsy):
    monkeypatch.setenv("KICAD_MCP_HEALTH_UI", falsy)
    health_ui.maybe_launch_health_ui()
    assert spawned == []


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "Yes", "on"])
def test_launches_once_when_env_set_and_none_running(monkeypatch, spawned, truthy):
    monkeypatch.setenv("KICAD_MCP_HEALTH_UI", truthy)
    monkeypatch.setattr(health_ui, "_health_ui_already_running", lambda: False)
    health_ui.maybe_launch_health_ui()
    assert len(spawned) == 1
    args = spawned[0][0]
    # Popen([<pythonw>, <mcp_health_monitor.py>])
    assert any("mcp_health_monitor.py" in str(part) for part in args)


def test_skips_when_already_running(monkeypatch, spawned):
    monkeypatch.setenv("KICAD_MCP_HEALTH_UI", "1")
    monkeypatch.setattr(health_ui, "_health_ui_already_running", lambda: True)
    health_ui.maybe_launch_health_ui()
    assert spawned == []


def test_popen_failure_is_swallowed(monkeypatch, caplog):
    monkeypatch.setenv("KICAD_MCP_HEALTH_UI", "1")
    monkeypatch.setattr(health_ui, "_health_ui_already_running", lambda: False)
    monkeypatch.setattr(health_ui.Path, "exists", lambda self: True)

    def _boom(*a, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(health_ui.subprocess, "Popen", _boom)
    # Must not raise.
    health_ui.maybe_launch_health_ui()
    assert any("non-fatal" in rec.message for rec in caplog.records)


def test_missing_script_is_noop(monkeypatch, spawned):
    monkeypatch.setenv("KICAD_MCP_HEALTH_UI", "1")
    monkeypatch.setattr(health_ui, "_health_ui_already_running", lambda: False)
    monkeypatch.setattr(health_ui.Path, "exists", lambda self: False)
    health_ui.maybe_launch_health_ui()
    assert spawned == []


def test_truthy_helper():
    assert health_ui._truthy("1")
    assert health_ui._truthy("  Yes ")
    assert not health_ui._truthy(None)
    assert not health_ui._truthy("0")
    assert not health_ui._truthy("maybe")


def test_already_running_scans_marker(monkeypatch):
    """_health_ui_already_running matches on the monitor script marker."""

    class _Proc:
        def __init__(self, cmdline):
            self.info = {"cmdline": cmdline}

    class _FakePsutil:
        NoSuchProcess = RuntimeError
        AccessDenied = RuntimeError

        @staticmethod
        def process_iter(fields):
            return [
                _Proc(["python.exe", "some_other.py"]),
                _Proc(["pythonw.exe", "D:/x/scripts/mcp_health_monitor.py"]),
            ]

    import sys

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    assert health_ui._health_ui_already_running() is True


def test_already_running_false_without_psutil(monkeypatch):
    import sys

    # Force the lazy `import psutil` inside the function to fail.
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert health_ui._health_ui_already_running() is False
