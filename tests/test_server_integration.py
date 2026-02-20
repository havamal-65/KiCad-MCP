"""Integration tests for the full server."""

from __future__ import annotations

import pytest

from kicad_mcp.config import BackendType, KiCadMCPConfig
from kicad_mcp.server import create_server


class TestServerCreation:
    def test_create_server_default(self):
        """Server creates with default config (file backend)."""
        config = KiCadMCPConfig(backend=BackendType.FILE, log_level="WARNING")
        mcp = create_server(config)
        assert mcp is not None

    def test_server_has_tools(self):
        config = KiCadMCPConfig(backend=BackendType.FILE, log_level="WARNING")
        mcp = create_server(config)
        tools = mcp._tool_manager._tools
        # Check all tool groups are registered
        assert "read_board" in tools
        assert "read_schematic" in tools
        assert "export_gerbers" in tools
        assert "search_symbols" in tools
        assert "run_drc" in tools
        assert "open_project" in tools
        assert "get_backend_info" in tools

    def test_server_tool_count(self):
        config = KiCadMCPConfig(backend=BackendType.FILE, log_level="WARNING")
        mcp = create_server(config)
        tools = mcp._tool_manager._tools
        # Should have 64 tools total (8+3+5+5+6+9+6+22)
        # board(8) + drc(3) + export(5) + routing(5) + library(6) + library_manage(9) + project(6) + schematic(22)
        assert len(tools) == 64

    def test_server_auto_backend(self):
        """Server creates with auto-detection."""
        config = KiCadMCPConfig(backend=BackendType.AUTO, log_level="WARNING")
        mcp = create_server(config)
        assert mcp is not None
