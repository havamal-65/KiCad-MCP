"""Smoke tests — confirm the integration harness works end-to-end.

Two tests: one verifies the bridge fixture itself yields a sane ping payload;
the other re-pings to confirm subsequent calls also succeed (catches one-shot
connection bugs in the harness).

If these fail, none of the other integration tests in this directory are
meaningful — fix the harness first.
"""

from __future__ import annotations

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration


def test_bridge_session_ping_payload(bridge_session):
    """The session fixture's initial ping returned a well-formed payload."""
    assert bridge_session["pong"] is True
    assert "kicad_version" in bridge_session
    assert isinstance(bridge_session["kicad_version"], str)


def test_bridge_ping_repeatable(bridge_session):
    """The bridge handles consecutive calls — not just the fixture's first one."""
    result = _tcp_call("ping", timeout=2.0)
    assert result["pong"] is True
