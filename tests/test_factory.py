"""Tests for backend factory and auto-detection."""

from __future__ import annotations

import pytest

from kicad_mcp.backends.factory import (
    _create_file_backend,
    create_composite_backend,
    get_available_backends,
)
from kicad_mcp.config import BackendType


def test_file_backend_always_available():
    backend = _create_file_backend()
    assert backend.is_available()
    assert backend.name == "file"


def test_create_composite_file_only():
    composite = create_composite_backend(BackendType.FILE)
    status = composite.get_status()
    assert status["primary_backend"] != "none"
    assert any(b["name"] == "file" for b in status["active_backends"])


def test_create_composite_auto():
    composite = create_composite_backend(BackendType.AUTO)
    status = composite.get_status()
    # File backend should always be present
    file_backends = [b for b in status["active_backends"] if b["name"] == "file"]
    assert len(file_backends) == 1


def test_get_available_backends():
    backends = get_available_backends()
    assert "file" in backends
    assert backends["file"]["available"] is True
    assert "ipc" in backends
    assert "swig" in backends
    assert "cli" in backends


def test_create_composite_ipc_not_available():
    """IPC backend should raise if explicitly requested but not available."""
    # This test will only pass if kipy is NOT installed
    try:
        import kipy
        pytest.skip("kipy is installed, IPC backend may be available")
    except ImportError:
        with pytest.raises(Exception):
            create_composite_backend(BackendType.IPC)
