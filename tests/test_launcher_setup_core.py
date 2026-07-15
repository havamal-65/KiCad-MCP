"""Unit tests for `launcher.setup_core` (U3, REQ-CORE-002).

Pure logic tested directly; the process/FS boundary is mocked. The ps1 and
`claude mcp` CLI legs are discharged live (V-M), not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from launcher import setup_core
from launcher.config import LauncherConfig
from launcher.processes import Result
from launcher.setup_core import (
    SetupItem,
    _classify_bridge,
    _parse_bridge_version,
    _parse_mcp_get,
    _registration_payload,
    setup_ok,
)


def _cfg(tmp_path: Path) -> LauncherConfig:
    return LauncherConfig(
        venv_python=tmp_path / "python.exe",
        venv_pythonw=tmp_path / "pythonw.exe",
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_config_path=tmp_path / ".mcp.dev.json",
        projects_roots=[],
        recents_path=tmp_path / "recents.json",
    )


def _item(key: str = "x", required: bool = True, status: str = "pass") -> SetupItem:
    return SetupItem(key, key, required, status, "", None)  # type: ignore[arg-type]


SOURCE = 'import x\n_BRIDGE_VERSION = "2.3.3-noboard"\nrest = 1\n'


# --- _parse_bridge_version ----------------------------------------------------

def test_parse_version_present():
    assert _parse_bridge_version(SOURCE) == "2.3.3-noboard"


def test_parse_version_absent():
    assert _parse_bridge_version("no version here\n") is None


def test_parse_version_garbage_unquoted():
    assert _parse_bridge_version("_BRIDGE_VERSION = 123\n") is None


def test_parse_version_not_at_line_start_ignored():
    assert _parse_bridge_version('#_BRIDGE_VERSION = "9.9"\n') is None


# --- _classify_bridge -----------------------------------------------------------

def test_classify_absent_is_fail():
    status, detail = _classify_bridge(SOURCE, None, poison=False)
    assert status == "fail"
    assert "not installed" in detail


def test_classify_version_mismatch_is_stale():
    installed = SOURCE.replace("2.3.3-noboard", "2.3.2")
    status, detail = _classify_bridge(SOURCE, installed, poison=False)
    assert status == "stale"
    assert "2.3.2" in detail and "2.3.3-noboard" in detail


def test_classify_content_drift_same_version_is_stale():
    installed = SOURCE + "extra_line = True\n"
    status, detail = _classify_bridge(SOURCE, installed, poison=False)
    assert status == "stale"
    assert "drift" in detail


def test_classify_poison_copy_is_stale_even_if_identical():
    status, detail = _classify_bridge(SOURCE, SOURCE, poison=True)
    assert status == "stale"
    assert "poison" in detail


def test_classify_match_is_pass():
    status, detail = _classify_bridge(SOURCE, SOURCE, poison=False)
    assert status == "pass"
    assert "2.3.3-noboard" in detail


def test_classify_line_ending_drift_is_still_pass():
    installed = SOURCE.replace("\n", "\r\n")
    status, _ = _classify_bridge(SOURCE, installed, poison=False)
    assert status == "pass"


# --- _registration_payload ------------------------------------------------------

def test_registration_payload_is_valid_json_with_http_url(tmp_path):
    import json

    payload = json.loads(_registration_payload(_cfg(tmp_path)))
    assert payload == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}


# --- _parse_mcp_get --------------------------------------------------------------

def test_parse_mcp_get_registered():
    out = "kicad:\n  Scope: Project config (shared via .mcp.json)\n  Status: ✔ Connected\n"
    status, detail = _parse_mcp_get(0, out)
    assert status == "pass"
    assert detail.startswith("Scope:")


def test_parse_mcp_get_registered_without_scope_line():
    status, detail = _parse_mcp_get(0, "something\n")
    assert status == "pass"
    assert detail == "registered"


def test_parse_mcp_get_not_registered():
    status, _ = _parse_mcp_get(1, "No MCP server found with name: kicad")
    assert status == "fail"


def test_parse_mcp_get_cli_absent():
    status, detail = _parse_mcp_get(None, "")
    assert status == "optional_missing"
    assert "claude CLI" in detail


# --- setup_ok ---------------------------------------------------------------------

def test_setup_ok_required_fail_blocks():
    assert setup_ok([_item(status="fail")]) is False


def test_setup_ok_required_stale_blocks():
    assert setup_ok([_item(status="stale")]) is False


def test_setup_ok_optional_missing_never_blocks():
    items = [_item(required=False, status="optional_missing"), _item(status="pass")]
    assert setup_ok(items) is True


def test_setup_ok_required_optional_missing_degrades_not_blocks():
    # claude_mcp without the CLI: required item, but absence of the optional
    # prerequisite must not block setup (REQ-WIZ-004).
    assert setup_ok([_item(key="claude_mcp", status="optional_missing")]) is True


def test_setup_ok_all_pass():
    assert setup_ok([_item(), _item(key="y", required=False)]) is True


# --- check_bridge (FS boundary via monkeypatched paths) -----------------------------

def _wire_bridge_paths(monkeypatch, source: Path, installed: Path, poison: list[Path]):
    monkeypatch.setattr(setup_core, "bridge_source_path", lambda: source)
    monkeypatch.setattr(setup_core, "installed_bridge_path", lambda: installed)
    monkeypatch.setattr(setup_core, "poison_copies", lambda: poison)


def test_check_bridge_absent(tmp_path, monkeypatch):
    src = tmp_path / "bridge.py"
    src.write_text(SOURCE, encoding="utf-8")
    _wire_bridge_paths(monkeypatch, src, tmp_path / "missing" / "__init__.py", [])
    item = setup_core.check_bridge()
    assert item.status == "fail"
    assert item.fix == "install_bridge"


def test_check_bridge_current(tmp_path, monkeypatch):
    src = tmp_path / "bridge.py"
    src.write_text(SOURCE, encoding="utf-8")
    inst = tmp_path / "installed" / "__init__.py"
    inst.parent.mkdir()
    inst.write_text(SOURCE, encoding="utf-8")
    _wire_bridge_paths(monkeypatch, src, inst, [])
    item = setup_core.check_bridge()
    assert item.status == "pass"
    assert item.fix is None


def test_check_bridge_stale_flags_fix(tmp_path, monkeypatch):
    src = tmp_path / "bridge.py"
    src.write_text(SOURCE, encoding="utf-8")
    inst = tmp_path / "installed" / "__init__.py"
    inst.parent.mkdir()
    inst.write_text(SOURCE.replace("2.3.3-noboard", "2.3.2"), encoding="utf-8")
    _wire_bridge_paths(monkeypatch, src, inst, [])
    item = setup_core.check_bridge()
    assert item.status == "stale"
    assert item.fix == "install_bridge"


def test_check_bridge_source_missing_is_unknown(tmp_path, monkeypatch):
    _wire_bridge_paths(
        monkeypatch, tmp_path / "nope.py", tmp_path / "installed" / "__init__.py", []
    )
    item = setup_core.check_bridge()
    assert item.status == "unknown"


# --- fixes: idempotency + boundary mocked -----------------------------------------

def test_fix_register_claude_noop_when_already_passing(tmp_path, monkeypatch):
    passing = SetupItem("claude_mcp", "Claude Code registration", True, "pass", "Scope: User", None)
    monkeypatch.setattr(setup_core, "check_claude_mcp", lambda cfg: passing)

    def _boom(*a, **k):
        raise AssertionError("subprocess must not be invoked on a no-op")

    monkeypatch.setattr(setup_core.subprocess, "run", _boom)
    outcome = setup_core.fix_register_claude(_cfg(tmp_path))
    assert outcome.item is passing
    assert "already configured" in outcome.message


def test_fix_register_claude_cli_absent(tmp_path, monkeypatch):
    missing = SetupItem("claude_mcp", "Claude Code registration", True, "optional_missing", "", None)
    monkeypatch.setattr(setup_core, "check_claude_mcp", lambda cfg: missing)
    monkeypatch.setattr(setup_core.shutil, "which", lambda name: None)
    outcome = setup_core.fix_register_claude(_cfg(tmp_path))
    assert "claude CLI not found" in outcome.message


def test_fix_install_bridge_rechecks(monkeypatch):
    monkeypatch.setattr(
        setup_core.processes, "reinstall_bridge",
        lambda: Result("bridge", "started", "bridge reinstalled — restart the PCB editor"),
    )
    fresh = SetupItem("bridge", "pcbnew bridge", True, "pass", "2.3.3-noboard — current", None)
    monkeypatch.setattr(setup_core, "check_bridge", lambda: fresh)
    outcome = setup_core.fix_install_bridge()
    assert outcome.item is fresh
    assert "restart the PCB editor" in outcome.message


def test_fix_install_bridge_failure_still_returns_recheck(monkeypatch):
    monkeypatch.setattr(
        setup_core.processes, "reinstall_bridge",
        lambda: Result("bridge", "failed", "pwsh not found on PATH"),
    )
    still_stale = SetupItem("bridge", "pcbnew bridge", True, "stale", "…", "install_bridge")
    monkeypatch.setattr(setup_core, "check_bridge", lambda: still_stale)
    outcome = setup_core.fix_install_bridge()
    assert outcome.item is still_stale
    assert "pwsh not found" in outcome.message


# --- collect_setup ordering ---------------------------------------------------------

def test_collect_setup_order_and_keys(tmp_path, monkeypatch):
    for name in ("check_kicad", "check_bridge", "check_claude_cli", "check_java"):
        key = name.removeprefix("check_")
        monkeypatch.setattr(setup_core, name, lambda k=key: _item(key=k))
    monkeypatch.setattr(setup_core, "check_claude_mcp", lambda cfg: _item(key="claude_mcp"))
    items = setup_core.collect_setup(_cfg(tmp_path))
    assert [i.key for i in items] == ["kicad", "bridge", "claude_cli", "claude_mcp", "java"]
