"""Tests for the CLI backend."""

from __future__ import annotations

import pytest

from kicad_mcp.backends.cli_backend import CLIBackend
from kicad_mcp.utils.platform_helper import find_kicad_cli


@pytest.fixture
def cli_backend() -> CLIBackend:
    return CLIBackend()


class TestCLIBackend:
    def test_name(self, cli_backend: CLIBackend):
        assert cli_backend.name == "cli"

    def test_availability_matches_cli_presence(self, cli_backend: CLIBackend):
        cli = find_kicad_cli()
        assert cli_backend.is_available() == (cli is not None)

    def test_capabilities(self, cli_backend: CLIBackend):
        from kicad_mcp.backends.base import BackendCapability
        caps = cli_backend.capabilities
        assert BackendCapability.DRC in caps
        assert BackendCapability.ERC in caps
        assert BackendCapability.EXPORT_GERBER in caps
        assert BackendCapability.EXPORT_DRILL in caps

    @pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
    def test_get_version(self, cli_backend: CLIBackend):
        version = cli_backend.get_version()
        assert version is not None
        assert len(version) > 0

    @pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
    def test_get_export_ops(self, cli_backend: CLIBackend):
        ops = cli_backend.get_export_ops()
        assert ops is not None

    @pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
    def test_get_drc_ops(self, cli_backend: CLIBackend):
        ops = cli_backend.get_drc_ops()
        assert ops is not None
