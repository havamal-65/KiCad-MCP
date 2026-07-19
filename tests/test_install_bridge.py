"""Tests for the `kicad-mcp install-bridge` CLI (REQ-INST-002/004).

Focus: the Windows Documents-dir defect fix. The old code targeted
``USERPROFILE\\Documents``; on machines where Windows redirects Documents
(OneDrive Known Folder Move) that is a directory KiCad never scans, so the
bridge landed where pcbnew could not load it. The installer now resolves the
3rdparty root through the shared resolver (config-first → OS known-folder).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp_plugin import install_bridge as ib


@pytest.fixture(autouse=True)
def _win32(monkeypatch):
    """Drive the Windows code path regardless of host OS."""
    monkeypatch.setattr(ib.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\redir\AppData\Roaming")
    yield


def test_detect_dirs_uses_known_folder_not_userprofile(monkeypatch, tmp_path):
    """The 3rdparty plugins root follows the OS-reported Documents dir, even
    when it diverges from USERPROFILE\\Documents (the redirected-Documents
    regression this fix pins)."""
    from kicad_mcp.utils import kicad_version as kv

    redirected = tmp_path / "OneDrive" / "Documents"
    monkeypatch.setattr(kv, "documents_dir", lambda: redirected)
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "no_config" / v)
    # USERPROFILE points somewhere else entirely — must NOT be used.
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))

    plugins_root, scripting_root, pcbnew_json = ib._detect_kicad_dirs("10.0")

    assert plugins_root == redirected / "KiCad" / "10.0" / "3rdparty" / "plugins"
    assert (tmp_path / "profile") not in plugins_root.parents
    # config/pcbnew.json stay under APPDATA, versioned.
    assert scripting_root == Path(
        r"C:\Users\redir\AppData\Roaming\kicad\10.0\scripting\plugins"
    )
    assert pcbnew_json == Path(
        r"C:\Users\redir\AppData\Roaming\kicad\10.0\pcbnew.json"
    )


def test_detect_dirs_config_first(monkeypatch, tmp_path):
    """A relocated KICAD10_3RD_PARTY wins over the Documents default."""
    from kicad_mcp.utils import kicad_version as kv

    cfg = tmp_path / "cfg" / "10.0"
    cfg.mkdir(parents=True)
    relocated = tmp_path / "relocated_3rd"
    (cfg / "kicad_common.json").write_text(
        json.dumps({"environment": {"vars": {"KICAD10_3RD_PARTY": str(relocated)}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "cfg" / v)

    plugins_root, _, _ = ib._detect_kicad_dirs("10.0")
    assert plugins_root == relocated / "plugins"


def test_detect_dirs_accepts_version_10(monkeypatch, tmp_path):
    from kicad_mcp.utils import kicad_version as kv

    monkeypatch.setattr(kv, "documents_dir", lambda: tmp_path / "Documents")
    monkeypatch.setattr(kv, "config_dir", lambda v: tmp_path / "no_config" / v)
    plugins_root, _, pcbnew_json = ib._detect_kicad_dirs("10.0")
    assert "10.0" in plugins_root.parts
    assert "10.0" in pcbnew_json.parts


def test_default_version_prefers_newest(monkeypatch):
    from kicad_mcp.utils import kicad_version as kv

    monkeypatch.setattr(
        kv, "preferred", lambda: kv.KicadInstall("10.0", Path("x"), Path("x"), None)
    )
    assert ib._default_version() == "10.0"


def test_all_versions_lists_every_install(monkeypatch):
    from kicad_mcp.utils import kicad_version as kv

    monkeypatch.setattr(
        kv,
        "installed",
        lambda refresh=False: [
            kv.KicadInstall("10.0", Path("a"), Path("a"), None),
            kv.KicadInstall("9.0", Path("b"), Path("b"), None),
        ],
    )
    assert ib._all_versions() == ["10.0", "9.0"]
