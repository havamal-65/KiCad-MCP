"""Tests for KiCadMCPConfig — F1 step 6: IPC settings wiring.

The ipc_* fields mirror the exact KICAD_MCP_IPC_* env vars that
ipc_connection.py reads at runtime; the consistency tests pin that the two
can never disagree for canonical values.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kicad_mcp.backends.ipc_connection import ipc_enabled
from kicad_mcp.config import BackendType, KiCadMCPConfig


def _config(**kwargs):
    # _env_file=None keeps a developer-local .env from polluting the tests
    return KiCadMCPConfig(_env_file=None, **kwargs)


class TestDefaults:
    def test_backend_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_BACKEND", raising=False)
        assert _config().backend == BackendType.AUTO

    def test_ipc_defaults(self, monkeypatch):
        for var in ("KICAD_MCP_IPC_ENABLED", "KICAD_MCP_IPC_SOCKET",
                    "KICAD_MCP_IPC_TIMEOUT_MS"):
            monkeypatch.delenv(var, raising=False)
        config = _config()
        assert config.ipc_enabled is True
        assert config.ipc_socket is None
        assert config.ipc_timeout_ms == 2000


class TestEnvOverrides:
    def test_ipc_enabled_kill_switch(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", "0")
        assert _config().ipc_enabled is False

    def test_ipc_socket(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_SOCKET", "ipc://custom/api.sock")
        assert _config().ipc_socket == "ipc://custom/api.sock"

    def test_ipc_timeout(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_TIMEOUT_MS", "5000")
        assert _config().ipc_timeout_ms == 5000

    def test_ipc_timeout_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_TIMEOUT_MS", "0")
        with pytest.raises(ValidationError):
            _config()

    def test_backend_ipc_value_accepted(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_BACKEND", "ipc")
        assert _config().backend == BackendType.IPC


class TestRuntimeConsistency:
    """config.ipc_enabled and ipc_connection.ipc_enabled() read the SAME env
    var — for canonical values they must always agree."""

    @pytest.mark.parametrize("value,expected", [
        ("0", False), ("false", False), ("no", False), ("off", False),
        ("1", True), ("true", True), ("yes", True), ("on", True),
    ])
    def test_agreement_on_canonical_values(self, monkeypatch, value, expected):
        monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", value)
        assert _config().ipc_enabled is expected
        assert ipc_enabled() is expected

    def test_agreement_when_unset(self, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_IPC_ENABLED", raising=False)
        assert _config().ipc_enabled is True
        assert ipc_enabled() is True
