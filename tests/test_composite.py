"""Tests for CompositeBackend capability routing and priority."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kicad_mcp.backends.base import BackendCapability, BoardOps, KiCadBackend
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.models.errors import CapabilityNotSupportedError


# ---------------------------------------------------------------------------
# Minimal mock backend helpers
# ---------------------------------------------------------------------------

def _make_backend(
    name: str,
    capabilities: set[BackendCapability],
    available: bool = True,
    board_ops: BoardOps | None = None,
) -> KiCadBackend:
    """Create a simple mock KiCadBackend."""
    backend = MagicMock(spec=KiCadBackend)
    backend.name = name
    backend.capabilities = capabilities
    backend.is_available.return_value = available
    backend.get_board_ops.return_value = board_ops
    return backend


# ---------------------------------------------------------------------------
# Capability routing
# ---------------------------------------------------------------------------

def test_file_backend_is_default_for_board_read():
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})
    composite = CompositeBackend([file_backend])
    resolved = composite._get_backend_for(BackendCapability.BOARD_READ)
    assert resolved.name == "file"


def test_higher_priority_backend_wins_for_board_read():
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ, BackendCapability.BOARD_MODIFY})
    plugin_backend = _make_backend("plugin", {BackendCapability.BOARD_READ, BackendCapability.BOARD_MODIFY})

    composite = CompositeBackend([file_backend, plugin_backend])
    resolved = composite._get_backend_for(BackendCapability.BOARD_READ)
    assert resolved.name == "plugin"


def test_lower_priority_backend_used_for_unsupported_capability():
    plugin_backend = _make_backend("plugin", {BackendCapability.BOARD_READ})
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ, BackendCapability.SCHEMATIC_READ})

    composite = CompositeBackend([plugin_backend, file_backend])
    # plugin doesn't have SCHEMATIC_READ; file does
    resolved = composite._get_backend_for(BackendCapability.SCHEMATIC_READ)
    assert resolved.name == "file"


def test_missing_capability_raises():
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})
    composite = CompositeBackend([file_backend])
    with pytest.raises(CapabilityNotSupportedError):
        composite._get_backend_for(BackendCapability.BOARD_ROUTE)


def test_has_capability_true_when_present():
    backend = _make_backend("file", {BackendCapability.BOARD_READ})
    composite = CompositeBackend([backend])
    assert composite.has_capability(BackendCapability.BOARD_READ) is True


def test_has_capability_false_when_absent():
    backend = _make_backend("file", {BackendCapability.BOARD_READ})
    composite = CompositeBackend([backend])
    assert composite.has_capability(BackendCapability.ZONE_REFILL) is False


# ---------------------------------------------------------------------------
# Plugin watchdog dynamic promotion
# ---------------------------------------------------------------------------

def test_plugin_watchdog_promoted_when_becomes_available():
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})
    plugin_watchdog = _make_backend("plugin", {BackendCapability.BOARD_READ, BackendCapability.BOARD_MODIFY},
                                    available=False)

    composite = CompositeBackend([file_backend], plugin_watchdog=plugin_watchdog)

    # Initially plugin is unavailable — file backend wins
    resolved = composite._get_backend_for(BackendCapability.BOARD_READ)
    assert resolved.name == "file"

    # Now plugin becomes available
    plugin_watchdog.is_available.return_value = True
    resolved = composite._get_backend_for(BackendCapability.BOARD_READ)
    assert resolved.name == "plugin"


def test_plugin_watchdog_not_added_when_unavailable():
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})
    plugin_watchdog = _make_backend("plugin", {BackendCapability.BOARD_READ}, available=False)

    composite = CompositeBackend([file_backend], plugin_watchdog=plugin_watchdog)
    composite._get_backend_for(BackendCapability.BOARD_READ)
    assert plugin_watchdog not in composite._backends


# ---------------------------------------------------------------------------
# export_dsn / import_ses delegation
# ---------------------------------------------------------------------------

def test_export_dsn_routes_to_board_route_backend():
    mock_ops = MagicMock()
    mock_ops.export_dsn.return_value = {"status": "ok"}

    route_backend = _make_backend("pcbnew_subprocess", {BackendCapability.BOARD_ROUTE},
                                  board_ops=mock_ops)
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})

    composite = CompositeBackend([file_backend, route_backend])
    result = composite.export_dsn(Path("board.kicad_pcb"), Path("board.dsn"))
    assert result == {"status": "ok"}
    mock_ops.export_dsn.assert_called_once()


def test_import_ses_routes_to_board_route_backend():
    mock_ops = MagicMock()
    mock_ops.import_ses.return_value = {"status": "ok"}

    route_backend = _make_backend("pcbnew_subprocess", {BackendCapability.BOARD_ROUTE},
                                  board_ops=mock_ops)
    file_backend = _make_backend("file", {BackendCapability.BOARD_READ})

    composite = CompositeBackend([file_backend, route_backend])
    result = composite.import_ses(Path("board.kicad_pcb"), Path("board.ses"))
    assert result == {"status": "ok"}
    mock_ops.import_ses.assert_called_once()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_lists_all_backends():
    b1 = _make_backend("file", {BackendCapability.BOARD_READ})
    b2 = _make_backend("cli", {BackendCapability.EXPORT_GERBER})
    composite = CompositeBackend([b1, b2])
    status = composite.get_status()
    names = {b["name"] for b in status["active_backends"]}
    assert "file" in names
    assert "cli" in names


def test_get_status_capability_routing_populated():
    backend = _make_backend("file", {BackendCapability.BOARD_READ, BackendCapability.SCHEMATIC_READ})
    composite = CompositeBackend([backend])
    status = composite.get_status()
    routing = status["capability_routing"]
    assert "BOARD_READ" in routing
    assert routing["BOARD_READ"] == "file"
