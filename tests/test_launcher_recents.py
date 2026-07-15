"""Unit tests for launcher.recents (Stack Launcher M2 / REQ-TEST-001)."""

from __future__ import annotations

from pathlib import Path

import pytest

from launcher.config import LauncherConfig
from launcher import recents


def _cfg(tmp_path: Path, roots: list[Path] | None = None) -> LauncherConfig:
    return LauncherConfig(
        venv_python=tmp_path / "python.exe",
        venv_pythonw=tmp_path / "pythonw.exe",
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_config_path=tmp_path / ".mcp.dev.json",
        projects_roots=roots or [],
        recents_path=tmp_path / "store" / "recents.json",
    )


def _board(tmp_path: Path, name: str) -> Path:
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text("(kicad_pcb)", encoding="utf-8")
    return p


def test_promote_moves_to_front_and_sets_last_used(tmp_path):
    cfg = _cfg(tmp_path)
    a = _board(tmp_path, "alpha")
    b = _board(tmp_path, "beta")
    recents.promote(cfg, a)
    recents.promote(cfg, b)
    loaded = recents.load_recents(cfg)
    assert [e.name for e in loaded] == ["beta", "alpha"]
    assert loaded[0].last_used >= loaded[1].last_used
    assert loaded[0].last_used > 0


def test_promote_dedupes_same_path(tmp_path):
    cfg = _cfg(tmp_path)
    a = _board(tmp_path, "alpha")
    recents.promote(cfg, a)
    recents.promote(cfg, a)
    loaded = recents.load_recents(cfg)
    assert len(loaded) == 1


def test_second_load_preorders_most_recent(tmp_path):
    cfg = _cfg(tmp_path)
    a = _board(tmp_path, "alpha")
    b = _board(tmp_path, "beta")
    recents.promote(cfg, a)
    recents.promote(cfg, b)
    items = recents.list_for_picker(cfg)
    assert [i.name for i in items] == ["beta", "alpha"]


def test_list_for_picker_prunes_missing_paths(tmp_path):
    cfg = _cfg(tmp_path)
    a = _board(tmp_path, "alpha")
    recents.promote(cfg, a)
    gone = tmp_path / "gone.kicad_pcb"
    recents.promote(cfg, gone)  # never created on disk
    items = recents.list_for_picker(cfg)
    names = [i.name for i in items]
    assert "gone" not in names
    assert "alpha" in names


def test_corrupt_json_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.recents_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.recents_path.write_text("{ this is not json", encoding="utf-8")
    assert recents.load_recents(cfg) == []


def test_missing_file_returns_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert recents.load_recents(cfg) == []


def test_atomic_write_leaves_no_partial_file(tmp_path):
    cfg = _cfg(tmp_path)
    a = _board(tmp_path, "alpha")
    recents.promote(cfg, a)
    store = cfg.recents_path.parent
    leftovers = [p.name for p in store.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert cfg.recents_path.exists()


def test_discover_projects_finds_boards_skips_backups(tmp_path):
    root = tmp_path / "projects"
    (root / "proj_a").mkdir(parents=True)
    (root / "proj_a" / "a.kicad_pcb").write_text("x", encoding="utf-8")
    backups = root / "proj_a" / "proj_a-backups"
    backups.mkdir()
    (backups / "a.kicad_pcb").write_text("x", encoding="utf-8")
    found = recents.discover_projects([root])
    assert len(found) == 1
    assert found[0].name == "a.kicad_pcb"


def test_discover_projects_skips_mcp_backup_snapshots(tmp_path):
    root = tmp_path / "projects"
    (root / "proj_a").mkdir(parents=True)
    (root / "proj_a" / "a.kicad_pcb").write_text("x", encoding="utf-8")
    mcp_backups = root / "proj_a" / ".kicad_mcp_backups"
    mcp_backups.mkdir()
    (mcp_backups / "a_20260703_221749.kicad_pcb").write_text("x", encoding="utf-8")
    found = recents.discover_projects([root])
    assert [p.name for p in found] == ["a.kicad_pcb"]


def test_list_for_picker_unions_discovered(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    disc = root / "discovered.kicad_pcb"
    disc.write_text("x", encoding="utf-8")
    cfg = _cfg(tmp_path, roots=[root])
    a = _board(tmp_path, "alpha")
    recents.promote(cfg, a)
    items = recents.list_for_picker(cfg)
    names = {i.name for i in items}
    assert "alpha" in names and "discovered" in names
    # Recents come first.
    assert items[0].name == "alpha"
