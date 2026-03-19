"""Plugin backend — communicates with the kicad_mcp_bridge KiCad plugin.

The bridge plugin runs inside KiCad's embedded Python interpreter and starts a
local TCP server on localhost:9760.  This backend connects to that server to
read live board data via the pcbnew API, with no gRPC and no file-parsing.

Port:    KICAD_MCP_PLUGIN_PORT  (default 9760)
Timeout: KICAD_MCP_PLUGIN_TIMEOUT  (default 2.0s for is_available, 10.0s for ops)
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import BackendCapability, BoardOps, KiCadBackend
from kicad_mcp.logging_config import get_logger

logger = get_logger("backend.plugin")

_DEFAULT_PORT = 9760
_DEFAULT_PING_TIMEOUT = 2.0
_DEFAULT_OP_TIMEOUT = 10.0


def _get_port() -> int:
    return int(os.environ.get("KICAD_MCP_PLUGIN_PORT", str(_DEFAULT_PORT)))


def _get_ping_timeout() -> float:
    return float(os.environ.get("KICAD_MCP_PLUGIN_TIMEOUT", str(_DEFAULT_PING_TIMEOUT)))


def _get_op_timeout() -> float:
    # Ops get 5× the ping timeout, or a configured override
    return float(os.environ.get("KICAD_MCP_PLUGIN_OP_TIMEOUT", str(_DEFAULT_OP_TIMEOUT)))


# ---------------------------------------------------------------------------
# Board ops
# ---------------------------------------------------------------------------

class PluginBoardOps(BoardOps):
    """Board operations via the kicad_mcp_bridge plugin TCP server."""

    def _call(self, method: str, path: str | None = None, **kwargs) -> Any:
        """Send a JSON request to the bridge and return the result payload.

        Raises:
            ConnectionRefusedError: Bridge not running.
            RuntimeError: Bridge returned an error response.
        """
        port = _get_port()
        timeout = _get_op_timeout()
        request = {"method": method}
        if path is not None:
            request["path"] = str(path)
        request.update(kwargs)

        with socket.create_connection(("localhost", port), timeout=timeout) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            # Read response line
            data = b""
            sock.settimeout(timeout)
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk

        response = json.loads(data.decode("utf-8").strip())
        if response.get("status") == "error":
            raise RuntimeError(f"Plugin bridge error: {response.get('message', 'unknown')}")
        return response.get("result")

    def get_board_info(self, path: Path) -> dict[str, Any]:
        return self._call("get_board_info", path)

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return self._call("get_components", path)

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return self._call("get_nets", path)

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        raise NotImplementedError("get_tracks is out of POC scope for plugin backend")

    def read_board(self, path: Path) -> dict[str, Any]:
        info = self.get_board_info(path)
        components = self.get_components(path)
        nets = self.get_nets(path)
        return {"info": info, "components": components, "nets": nets, "tracks": []}


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class PluginBackend(KiCadBackend):
    """KiCad backend that talks to the in-process kicad_mcp_bridge plugin."""

    name = "plugin"
    capabilities = {BackendCapability.BOARD_READ}

    # Availability cache
    _cache_result: bool | None = None
    _cache_ts: float = 0.0
    _CACHE_TTL: float = 5.0

    def is_available(self) -> bool:
        now = time.monotonic()
        if self._cache_result is not None and (now - self._cache_ts) < self._CACHE_TTL:
            return self._cache_result

        available = self._probe()
        self._cache_result = available
        self._cache_ts = now
        return available

    def _probe(self) -> bool:
        """Try a ping request; return True if bridge responds."""
        port = _get_port()
        timeout = _get_ping_timeout()
        try:
            with socket.create_connection(("localhost", port), timeout=timeout) as sock:
                request = json.dumps({"method": "ping"}) + "\n"
                sock.sendall(request.encode("utf-8"))
                data = b""
                sock.settimeout(timeout)
                while b"\n" not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            response = json.loads(data.decode("utf-8").strip())
            return response.get("result", {}).get("pong") is True
        except (ConnectionRefusedError, OSError, json.JSONDecodeError, TimeoutError):
            return False

    def get_board_ops(self) -> PluginBoardOps:
        return PluginBoardOps()

    def get_version(self) -> str | None:
        """Return KiCad version string reported by the bridge, or None."""
        port = _get_port()
        timeout = _get_ping_timeout()
        try:
            with socket.create_connection(("localhost", port), timeout=timeout) as sock:
                sock.sendall((json.dumps({"method": "ping"}) + "\n").encode("utf-8"))
                data = b""
                sock.settimeout(timeout)
                while b"\n" not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            response = json.loads(data.decode("utf-8").strip())
            return response.get("result", {}).get("kicad_version")
        except Exception:
            return None
