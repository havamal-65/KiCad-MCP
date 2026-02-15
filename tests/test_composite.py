"""Tests for the composite backend routing."""

from __future__ import annotations

import pytest

from kicad_mcp.backends.base import BackendCapability
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.models.errors import CapabilityNotSupportedError

from .conftest import MockBackend


def test_composite_routes_to_backend(mock_composite: CompositeBackend):
    ops = mock_composite.get_board_ops()
    assert ops is not None


def test_composite_has_capability(mock_composite: CompositeBackend):
    assert mock_composite.has_capability(BackendCapability.BOARD_READ)
    assert mock_composite.has_capability(BackendCapability.SCHEMATIC_READ)


def test_composite_missing_capability():
    """Backend with no DRC should raise for DRC ops."""
    backend = MockBackend(
        caps={BackendCapability.BOARD_READ, BackendCapability.SCHEMATIC_READ}
    )
    composite = CompositeBackend([backend])
    assert not composite.has_capability(BackendCapability.DRC)
    with pytest.raises(CapabilityNotSupportedError):
        composite.get_drc_ops()


def test_composite_priority_routing():
    """Higher-priority backend should win for shared capabilities."""
    low = MockBackend(name_str="file", caps={BackendCapability.BOARD_READ})
    high = MockBackend(name_str="ipc", caps={BackendCapability.BOARD_READ})
    composite = CompositeBackend([low, high])

    status = composite.get_status()
    routing = status["capability_routing"]
    assert routing["BOARD_READ"] == "ipc"


def test_composite_status(mock_composite: CompositeBackend):
    status = mock_composite.get_status()
    assert "active_backends" in status
    assert "primary_backend" in status
    assert "capability_routing" in status
    assert len(status["active_backends"]) > 0


def test_composite_get_all_ops(mock_composite: CompositeBackend):
    assert mock_composite.get_board_ops() is not None
    assert mock_composite.get_schematic_ops() is not None
    assert mock_composite.get_export_ops() is not None
    assert mock_composite.get_drc_ops() is not None
    assert mock_composite.get_library_ops() is not None
