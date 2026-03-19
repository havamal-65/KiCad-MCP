"""Tests for PluginBackend — no KiCad installation required.

A lightweight mock TCP server is started in a daemon thread for each test that
needs one.  The KICAD_MCP_PLUGIN_PORT env-var is patched so the backend under
test connects to the mock instead of a real KiCad process.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from pathlib import Path
from typing import Any

import pytest

BOARD_PATH = "/tmp/test.kicad_pcb"

# ---------------------------------------------------------------------------
# Canned server responses
# ---------------------------------------------------------------------------

_PING_RESPONSE = {
    "status": "ok",
    "result": {"pong": True, "kicad_version": "9.0.0"},
}

_BOARD_INFO_RESPONSE = {
    "status": "ok",
    "result": {
        "title": "Test Board",
        "revision": "1",
        "layer_count": 2,
        "width_mm": 50.0,
        "height_mm": 40.0,
        "net_count": 5,
        "footprint_count": 3,
    },
}

_COMPONENTS_RESPONSE = {
    "status": "ok",
    "result": [
        {"reference": "R1", "value": "10k", "x": 10.0, "y": 20.0, "layer": "F.Cu", "rotation": 0.0},
        {"reference": "C1", "value": "100n", "x": 30.0, "y": 20.0, "layer": "F.Cu", "rotation": 90.0},
    ],
}

_NETS_RESPONSE = {
    "status": "ok",
    "result": [
        {"net_id": 0, "name": ""},
        {"net_id": 1, "name": "GND"},
        {"net_id": 2, "name": "VCC"},
    ],
}

_DISPATCH: dict[str, dict[str, Any]] = {
    "ping": _PING_RESPONSE,
    "get_board_info": _BOARD_INFO_RESPONSE,
    "get_components": _COMPONENTS_RESPONSE,
    "get_nets": _NETS_RESPONSE,
}


# ---------------------------------------------------------------------------
# Mock server
# ---------------------------------------------------------------------------

class _MockRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline()
            if not raw:
                return
            request = json.loads(raw.decode("utf-8").strip())
            method = request.get("method", "")
            response = _DISPATCH.get(method, {"status": "error", "message": f"unknown method: {method}"})
        except Exception as exc:
            response = {"status": "error", "message": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


@pytest.fixture()
def mock_plugin_server(monkeypatch):
    """Spin up a mock TCP server on a random free port and patch the env var."""
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("localhost", 0), _MockRequestHandler)
    port = server.server_address[1]

    monkeypatch.setenv("KICAD_MCP_PLUGIN_PORT", str(port))
    # Also reset the backend availability cache so each test starts fresh
    import kicad_mcp.backends.plugin_backend as pb
    pb.PluginBackend._cache_result = None
    pb.PluginBackend._cache_ts = 0.0

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPluginBackendAvailability:
    def test_is_available_true(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBackend
        backend = PluginBackend()
        assert backend.is_available() is True

    def test_is_available_false(self, monkeypatch):
        """With no server running, is_available should return False (not raise)."""
        monkeypatch.setenv("KICAD_MCP_PLUGIN_PORT", "19760")  # nothing listening here
        import kicad_mcp.backends.plugin_backend as pb
        pb.PluginBackend._cache_result = None
        pb.PluginBackend._cache_ts = 0.0
        backend = pb.PluginBackend()
        assert backend.is_available() is False

    def test_name(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBackend
        assert PluginBackend.name == "plugin"

    def test_has_board_read_capability(self, mock_plugin_server):
        from kicad_mcp.backends.base import BackendCapability
        from kicad_mcp.backends.plugin_backend import PluginBackend
        assert BackendCapability.BOARD_READ in PluginBackend.capabilities


class TestPluginBoardOps:
    def test_get_board_info(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        result = ops.get_board_info(Path(BOARD_PATH))
        assert result["title"] == "Test Board"
        assert result["layer_count"] == 2
        assert result["footprint_count"] == 3

    def test_get_components(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        result = ops.get_components(Path(BOARD_PATH))
        assert isinstance(result, list)
        assert len(result) == 2
        refs = {c["reference"] for c in result}
        assert "R1" in refs
        assert "C1" in refs

    def test_get_nets(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        result = ops.get_nets(Path(BOARD_PATH))
        assert isinstance(result, list)
        names = {n["name"] for n in result}
        assert "GND" in names
        assert "VCC" in names

    def test_read_board_combines_all(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        result = ops.read_board(Path(BOARD_PATH))
        assert "info" in result
        assert "components" in result
        assert "nets" in result
        assert "tracks" in result
        assert result["info"]["title"] == "Test Board"

    def test_get_tracks_raises_not_implemented(self, mock_plugin_server):
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        with pytest.raises(NotImplementedError):
            ops.get_tracks(Path(BOARD_PATH))

    def test_server_unreachable_raises(self, monkeypatch):
        """_call raises ConnectionRefusedError when no server is listening."""
        monkeypatch.setenv("KICAD_MCP_PLUGIN_PORT", "19761")
        from kicad_mcp.backends.plugin_backend import PluginBoardOps
        ops = PluginBoardOps()
        with pytest.raises((ConnectionRefusedError, OSError)):
            ops.get_board_info(Path(BOARD_PATH))


class TestPluginBackendInFactory:
    def test_get_available_backends_includes_plugin(self):
        from kicad_mcp.backends.factory import get_available_backends
        backends = get_available_backends()
        assert "plugin" in backends
        assert "available" in backends["plugin"]
