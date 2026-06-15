"""Client-side identity validation for the #13B handshake.

Covers _validate_bridge_identity and its use in PluginDirectBackend._probe:
a pcbnew bridge is accepted, a non-pcbnew owner (project manager holding the
port) is rejected, and a legacy bridge with no identity payload still works.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBackend,
    _validate_bridge_identity,
)


# ---------------------------------------------------------------------------
# _validate_bridge_identity
# ---------------------------------------------------------------------------

def test_pcbnew_identity_accepted():
    # Should not raise.
    _validate_bridge_identity({"pong": True, "app": "pcbnew", "pid": 1234})


def test_legacy_identity_without_app_accepted():
    # Pre-#13B bridges omit the app field — must remain usable.
    _validate_bridge_identity({"pong": True, "kicad_version": "9.0.1"})


def test_wrong_owner_rejected():
    with pytest.raises(BridgeTemporarilyUnavailableError) as exc:
        _validate_bridge_identity({"pong": True, "app": "kicad", "pid": 4321})
    msg = str(exc.value)
    assert "kicad" in msg
    assert "4321" in msg
    assert "project manager" in msg.lower()


def test_non_dict_ignored():
    _validate_bridge_identity(None)
    _validate_bridge_identity("nope")


# ---------------------------------------------------------------------------
# PluginDirectBackend._probe — identity-aware availability
# ---------------------------------------------------------------------------

def _backend() -> PluginBackend:
    # Bypass __init__ side effects; _probe only needs the bound method.
    return PluginBackend.__new__(PluginBackend)


def test_probe_true_for_pcbnew_bridge():
    backend = _backend()
    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call",
        return_value={"pong": True, "app": "pcbnew", "pid": 1},
    ):
        assert backend._probe() is True


def test_probe_false_for_wrong_owner():
    backend = _backend()
    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call",
        return_value={"pong": True, "app": "kicad", "pid": 9},
    ):
        assert backend._probe() is False


def test_probe_true_for_legacy_bridge():
    backend = _backend()
    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call",
        return_value={"pong": True, "kicad_version": "9.0.1"},
    ):
        assert backend._probe() is True


def test_probe_false_when_unreachable():
    backend = _backend()
    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call",
        side_effect=BridgeTemporarilyUnavailableError("down"),
    ):
        assert backend._probe() is False
