"""Unit tests for the bridge's #13B bind-guard + identity handshake.

The bridge module cannot import the kicad_mcp package and runs inside KiCad's
embedded Python.  We load it via importlib (pcbnew import fails gracefully in
CI, so the TCP server never binds) and exercise the pure-Python helpers:

- _should_bind()  — only bind the TCP port inside the pcbnew editor
- _handle_ping()  — returns the identity payload the client validates
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("kicad_mcp_bridge_identity_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _should_bind — process classification
# ---------------------------------------------------------------------------

def test_should_bind_true_when_executable_is_pcbnew(bridge, monkeypatch):
    # wx is unavailable in CI, so detection falls through to the executable name.
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\KiCad\9.0\bin\pcbnew.exe")
    assert bridge._should_bind() is True


def test_should_bind_false_when_executable_is_project_manager(bridge, monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\KiCad\9.0\bin\kicad.exe")
    assert bridge._should_bind() is False


def test_should_bind_default_true_when_unclassifiable(bridge, monkeypatch):
    # A plain python interpreter matches neither pcbnew nor kicad → legacy default.
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    assert bridge._should_bind() is True


# ---------------------------------------------------------------------------
# _handle_ping — identity payload
# ---------------------------------------------------------------------------

def test_handle_ping_returns_identity_fields(bridge):
    result = bridge._handle_ping()

    assert result["pong"] is True
    assert result["app"] == "pcbnew"
    assert isinstance(result["pid"], int) and result["pid"] > 0
    assert result["bridge_version"] == bridge._BRIDGE_VERSION
    # pcbnew is not importable in CI → version unknown, no board loaded.
    assert result["kicad_version"] == "unknown"
    assert result["board_path"] is None
