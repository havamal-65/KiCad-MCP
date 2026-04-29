"""Tests for create_composite_backend factory function."""

from __future__ import annotations

import pytest

from kicad_mcp.backends.base import BackendCapability
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.backends.factory import create_composite_backend
from kicad_mcp.config import BackendType


# ---------------------------------------------------------------------------
# BackendType.FILE — safe to run without KiCad installed
# ---------------------------------------------------------------------------

def test_file_backend_returns_composite():
    result = create_composite_backend(BackendType.FILE)
    assert isinstance(result, CompositeBackend)


def test_file_backend_has_board_read():
    result = create_composite_backend(BackendType.FILE)
    assert result.has_capability(BackendCapability.BOARD_READ)


def test_file_backend_has_schematic_read():
    result = create_composite_backend(BackendType.FILE)
    assert result.has_capability(BackendCapability.SCHEMATIC_READ)


def test_file_backend_no_board_modify():
    """File backend is read-only; board writes require plugin/IPC."""
    result = create_composite_backend(BackendType.FILE)
    assert not result.has_capability(BackendCapability.BOARD_MODIFY)


def test_file_backend_no_zone_refill():
    """File backend cannot refill zones (needs pcbnew)."""
    result = create_composite_backend(BackendType.FILE)
    assert not result.has_capability(BackendCapability.ZONE_REFILL)


def test_file_backend_no_real_time_sync():
    """File backend has no IPC, so REAL_TIME_SYNC is absent."""
    result = create_composite_backend(BackendType.FILE)
    assert not result.has_capability(BackendCapability.REAL_TIME_SYNC)


def test_file_backend_has_library_search():
    result = create_composite_backend(BackendType.FILE)
    assert result.has_capability(BackendCapability.LIBRARY_SEARCH)


def test_file_backend_get_status_includes_file():
    result = create_composite_backend(BackendType.FILE)
    status = result.get_status()
    names = {b["name"] for b in status["active_backends"]}
    assert "file" in names


def test_file_backend_primary_backend_is_file():
    result = create_composite_backend(BackendType.FILE)
    status = result.get_status()
    assert status["primary_backend"] == "file"
