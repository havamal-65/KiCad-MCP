"""Unit tests for the KiCad version resolver (REQ-VER-006).

Enumeration runs against fake directory trees — no real KiCad required. Each
test resets the module's install cache and preferred-override so ordering and
env behavior are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp.utils import kicad_version as kv


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear resolver caches and force the Windows code path for path shape."""
    monkeypatch.setattr(kv, "_installed_cache", None)
    monkeypatch.setattr(kv, "_preferred_override", None)
    monkeypatch.delenv(kv.ENV_PREFERRED, raising=False)
    monkeypatch.setattr(kv, "_platform", lambda: "windows")
    yield


def _make_install(program_files: Path, version: str, *, cli: bool = True) -> None:
    """Create a fake KiCad install tree under a Program Files root."""
    bin_dir = program_files / "KiCad" / version / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "pcbnew.exe").write_text("", encoding="utf-8")
    if cli:
        (bin_dir / "kicad-cli.exe").write_text("", encoding="utf-8")


@pytest.fixture
def program_files(tmp_path, monkeypatch) -> Path:
    pf = tmp_path / "Program Files"
    pf.mkdir()
    monkeypatch.setattr(kv, "_program_files_roots", lambda: [pf])
    return pf


# --- enumeration -------------------------------------------------------------

def test_enumerate_multi_version_newest_first(program_files):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    _make_install(program_files, "8.0")
    installs = kv.installed()
    assert [i.version for i in installs] == ["10.0", "9.0", "8.0"]
    assert all(i.pcbnew is not None for i in installs)


def test_enumerate_single_version(program_files):
    _make_install(program_files, "10.0")
    assert [i.version for i in kv.installed()] == ["10.0"]


def test_enumerate_none_installed(program_files):
    assert kv.installed() == []
    assert kv.preferred() is None


def test_version_dir_without_pcbnew_is_ignored(program_files):
    # A leftover version dir with no pcbnew.exe is not a usable install.
    (program_files / "KiCad" / "7.0" / "bin").mkdir(parents=True)
    _make_install(program_files, "10.0")
    assert [i.version for i in kv.installed()] == ["10.0"]


def test_kicad_cli_optional(program_files):
    _make_install(program_files, "10.0", cli=False)
    (inst,) = kv.installed()
    assert inst.pcbnew is not None
    assert inst.kicad_cli is None


def test_refresh_rescans(program_files):
    _make_install(program_files, "9.0")
    assert [i.version for i in kv.installed()] == ["9.0"]
    _make_install(program_files, "10.0")
    assert [i.version for i in kv.installed()] == ["9.0"]  # cached
    assert [i.version for i in kv.installed(refresh=True)] == ["10.0", "9.0"]


# --- selection ---------------------------------------------------------------

def test_preferred_defaults_to_newest(program_files):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    assert kv.preferred().version == "10.0"


def test_env_override_valid(program_files, monkeypatch):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    monkeypatch.setenv(kv.ENV_PREFERRED, "9.0")
    assert kv.preferred().version == "9.0"


def test_env_override_invalid_falls_back(program_files, monkeypatch):
    _make_install(program_files, "10.0")
    monkeypatch.setenv(kv.ENV_PREFERRED, "6.6")
    assert kv.preferred().version == "10.0"  # warns, never raises


def test_set_preferred_valid(program_files):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    kv.set_preferred("9.0")
    assert kv.preferred().version == "9.0"


def test_set_preferred_uninstalled_rejected(program_files):
    _make_install(program_files, "10.0")
    kv.set_preferred("8.0")  # not installed → ignored
    assert kv.preferred().version == "10.0"


def test_set_preferred_none_clears(program_files):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    kv.set_preferred("9.0")
    kv.set_preferred(None)
    assert kv.preferred().version == "10.0"


def test_env_beats_set_preferred(program_files, monkeypatch):
    _make_install(program_files, "9.0")
    _make_install(program_files, "10.0")
    kv.set_preferred("9.0")
    monkeypatch.setenv(kv.ENV_PREFERRED, "10.0")
    assert kv.preferred().version == "10.0"


# --- probe_order -------------------------------------------------------------

def test_probe_order_preferred_first_then_known_tail(program_files):
    _make_install(program_files, "10.0")
    _make_install(program_files, "9.0")
    # 10.0, 9.0 installed; 8.0/7.0 come from the known tail (config-only dirs).
    assert kv.probe_order() == ["10.0", "9.0", "8.0", "7.0"]


def test_probe_order_dedups_and_honors_override(program_files, monkeypatch):
    _make_install(program_files, "10.0")
    _make_install(program_files, "9.0")
    monkeypatch.setenv(kv.ENV_PREFERRED, "9.0")
    order = kv.probe_order()
    assert order[0] == "9.0"
    assert order == ["9.0", "10.0", "8.0", "7.0"]
    assert len(order) == len(set(order))


def test_probe_order_no_installs_is_known_versions(program_files):
    assert kv.probe_order() == kv.KNOWN_VERSIONS


# --- derived paths -----------------------------------------------------------

def test_config_dir_windows(program_files, monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    # Expected built by joining (not a backslash literal) so it equals the code's
    # output under any pathlib flavor — the assertion runs on Linux CI too.
    assert kv.config_dir("10.0") == Path(r"C:\Users\x\AppData\Roaming") / "kicad" / "10.0"


def test_documents_dir_known_folder(monkeypatch):
    monkeypatch.setattr(kv, "_known_folder_documents", lambda: Path(r"D:\Docs"))
    assert kv.documents_dir() == Path(r"D:\Docs")


def test_documents_dir_falls_back_to_home(monkeypatch):
    monkeypatch.setattr(kv, "_known_folder_documents", lambda: None)
    assert kv.documents_dir() == Path.home() / "Documents"


def test_third_party_dir_default(monkeypatch):
    monkeypatch.setattr(kv, "_platform", lambda: "windows")
    monkeypatch.setattr(kv, "documents_dir", lambda: Path(r"C:\Users\x\Documents"))
    monkeypatch.setattr(kv, "config_dir", lambda v: Path(r"C:\nope") / v)
    assert (
        kv.third_party_dir("10.0")
        == Path(r"C:\Users\x\Documents") / "KiCad" / "10.0" / "3rdparty"
    )


def test_third_party_dir_config_first(tmp_path, monkeypatch):
    """A relocated KICAD10_3RD_PARTY in kicad_common.json wins over the default."""
    cfg = tmp_path / "kicad" / "10.0"
    cfg.mkdir(parents=True)
    (cfg / "kicad_common.json").write_text(
        json.dumps({"environment": {"vars": {"KICAD10_3RD_PARTY": r"E:\my3rd"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "kicad" / v)
    monkeypatch.setattr(kv, "documents_dir", lambda: tmp_path / "Documents")
    assert kv.third_party_dir("10.0") == Path(r"E:\my3rd")
    assert kv.plugins_3rdparty_dir("10.0") == Path(r"E:\my3rd") / "plugins"


def test_third_party_dir_config_missing_key_uses_default(tmp_path, monkeypatch):
    cfg = tmp_path / "kicad" / "10.0"
    cfg.mkdir(parents=True)
    (cfg / "kicad_common.json").write_text(
        json.dumps({"environment": {"vars": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "kicad" / v)
    monkeypatch.setattr(kv, "documents_dir", lambda: tmp_path / "Documents")
    assert kv.third_party_dir("10.0") == tmp_path / "Documents" / "KiCad" / "10.0" / "3rdparty"


def test_third_party_dir_bad_json_uses_default(tmp_path, monkeypatch):
    cfg = tmp_path / "kicad" / "10.0"
    cfg.mkdir(parents=True)
    (cfg / "kicad_common.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "kicad" / v)
    monkeypatch.setattr(kv, "documents_dir", lambda: tmp_path / "Documents")
    assert kv.third_party_dir("10.0") == tmp_path / "Documents" / "KiCad" / "10.0" / "3rdparty"
