"""Tests for project-level backend operations (text variables)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_pro(path: Path, extra: dict | None = None) -> Path:
    """Write a minimal .kicad_pro JSON file and return its path."""
    data: dict = {"meta": {"version": 1}}
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def backend():
    """PluginDirectBackend with no live bridge (bridge probe returns False)."""
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend
    return PluginDirectBackend()


# ---------------------------------------------------------------------------
# get_text_variables
# ---------------------------------------------------------------------------

def test_get_text_variables_returns_dict(tmp_path: Path, backend):
    pro = _make_pro(tmp_path / "test.kicad_pro", {"text_variables": {"REV": "1.0", "TITLE": "PCB"}})
    result = backend.get_text_variables(str(pro))
    assert result["status"] == "success"
    assert result["variables"] == {"REV": "1.0", "TITLE": "PCB"}


def test_get_text_variables_missing_key(tmp_path: Path, backend):
    pro = _make_pro(tmp_path / "test.kicad_pro")
    result = backend.get_text_variables(str(pro))
    assert result["status"] == "success"
    assert result["variables"] == {}


def test_get_text_variables_file_not_found(tmp_path: Path, backend):
    result = backend.get_text_variables(str(tmp_path / "nonexistent.kicad_pro"))
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# set_text_variables
# ---------------------------------------------------------------------------

def test_set_text_variables_writes_file(tmp_path: Path, backend):
    pro = _make_pro(tmp_path / "test.kicad_pro")
    result = backend.set_text_variables(str(pro), {"REV": "2.0", "COMPANY": "ACME"})
    assert result["status"] == "success"
    assert result["count"] == 2
    saved = json.loads(pro.read_text(encoding="utf-8"))
    assert saved["text_variables"] == {"REV": "2.0", "COMPANY": "ACME"}


def test_set_text_variables_overwrites_existing(tmp_path: Path, backend):
    pro = _make_pro(tmp_path / "test.kicad_pro", {"text_variables": {"OLD": "value"}})
    backend.set_text_variables(str(pro), {"NEW": "value2"})
    saved = json.loads(pro.read_text(encoding="utf-8"))
    assert "OLD" not in saved["text_variables"]
    assert saved["text_variables"] == {"NEW": "value2"}


def test_set_text_variables_preserves_other_keys(tmp_path: Path, backend):
    pro = _make_pro(tmp_path / "test.kicad_pro", {"net_settings": {"classes": []}})
    backend.set_text_variables(str(pro), {"REV": "1"})
    saved = json.loads(pro.read_text(encoding="utf-8"))
    assert "net_settings" in saved
    assert saved["text_variables"] == {"REV": "1"}


def test_set_text_variables_file_not_found(tmp_path: Path, backend):
    result = backend.set_text_variables(str(tmp_path / "nonexistent.kicad_pro"), {"X": "1"})
    assert result["status"] == "error"
