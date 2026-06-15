"""Unit tests for #14B stale lock/autosave cleanup + is_pcbnew_running."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils import platform_helper
from kicad_mcp.utils.platform_helper import cleanup_stale_session_files, is_pcbnew_running


def _seed(tmp_path: Path) -> dict[str, Path]:
    files = {
        "lock_tilde": tmp_path / "~board.kicad_pcb.lck",
        "lock_plain": tmp_path / "board.kicad_pcb.lck",
        "autosave_prefix": tmp_path / "_autosave-board.kicad_pcb",
        "autosave_suffix": tmp_path / "board-autosave.kicad_sch",
        "board": tmp_path / "board.kicad_pcb",
        "schematic": tmp_path / "board.kicad_sch",
    }
    for p in files.values():
        p.write_text("x", encoding="utf-8")
    return files


def test_removes_lock_and_autosave_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_helper, "is_kicad_running", lambda: False)
    files = _seed(tmp_path)

    removed = cleanup_stale_session_files(tmp_path)

    removed_set = set(removed)
    assert str(files["lock_tilde"]) in removed_set
    assert str(files["lock_plain"]) in removed_set
    assert str(files["autosave_prefix"]) in removed_set
    assert str(files["autosave_suffix"]) in removed_set
    # Real design files must survive.
    assert files["board"].exists()
    assert files["schematic"].exists()
    # Removed files are actually gone.
    assert not files["lock_tilde"].exists()
    assert not files["autosave_prefix"].exists()


def test_no_removal_while_kicad_running(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_helper, "is_kicad_running", lambda: True)
    files = _seed(tmp_path)

    removed = cleanup_stale_session_files(tmp_path)

    assert removed == []
    # Nothing deleted — a live session's lock must not be touched.
    assert files["lock_tilde"].exists()
    assert files["autosave_prefix"].exists()


def test_missing_directory_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_helper, "is_kicad_running", lambda: False)
    assert cleanup_stale_session_files(tmp_path / "does-not-exist") == []


def test_no_double_count_on_overlapping_patterns(tmp_path, monkeypatch):
    # "*.lck" and "~*.lck" both match a tilde lock — it must appear once.
    monkeypatch.setattr(platform_helper, "is_kicad_running", lambda: False)
    (tmp_path / "~x.lck").write_text("x", encoding="utf-8")

    removed = cleanup_stale_session_files(tmp_path)

    assert removed == [str(tmp_path / "~x.lck")]


# ---------------------------------------------------------------------------
# is_pcbnew_running delegates to a pcbnew-only process check
# ---------------------------------------------------------------------------

def test_is_pcbnew_running_checks_pcbnew_only(monkeypatch):
    captured = {}

    def fake(names):
        captured["names"] = names
        return True

    monkeypatch.setattr(platform_helper, "_any_process_running", fake)
    assert is_pcbnew_running() is True
    assert captured["names"] == ("pcbnew",)
