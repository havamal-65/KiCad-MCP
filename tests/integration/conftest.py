"""Pytest fixtures for KiCad MCP integration tests.

These tests run against a real KiCad pcbnew process with the kicad_mcp_bridge
plugin loaded. They are skipped by default; opt in by setting
``KICAD_INTEGRATION=1`` and having pcbnew open with a board.

Run locally:
    1. Open pcbnew with any .kicad_pcb file (e.g. ``examples/`` or a fresh board)
    2. Verify the bridge log shows "TCP server started"
    3. ``$env:KICAD_INTEGRATION="1"; pytest tests/integration -v``

See ``tests/integration/README.md`` for full setup, including the CI flow.
"""

from __future__ import annotations

import os
import socket

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


def _integration_enabled() -> bool:
    return os.environ.get("KICAD_INTEGRATION", "").lower() in ("1", "true", "yes")


def _bridge_port() -> int:
    return int(os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760"))


def _bridge_reachable(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def bridge_session():
    """Session fixture: gates every integration test on a live bridge.

    Skips with a clear, actionable message when:
      - ``KICAD_INTEGRATION`` is unset (default for normal pytest runs)
      - The bridge TCP port is not reachable
      - The bridge responds but ``ping`` does not return ``{"pong": True}``

    Yields the result of the ``ping`` handler (kicad_version included).
    """
    if not _integration_enabled():
        pytest.skip(
            "Integration tests disabled. Set KICAD_INTEGRATION=1 and ensure "
            "pcbnew is running with the kicad_mcp_bridge plugin loaded."
        )

    port = _bridge_port()
    if not _bridge_reachable(port):
        pytest.skip(
            f"Bridge not reachable on localhost:{port}. "
            "Open pcbnew and verify the bridge log shows 'TCP server started'."
        )

    result = _tcp_call("ping", timeout=2.0)
    if not isinstance(result, dict) or not result.get("pong"):
        pytest.fail(
            f"Bridge reachable but ping returned unexpected result: {result!r}"
        )

    yield result
