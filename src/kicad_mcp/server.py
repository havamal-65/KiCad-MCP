"""FastMCP server creation and tool/resource registration."""

from __future__ import annotations

from fastmcp import FastMCP

from kicad_mcp import __version__
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.backends.factory import create_composite_backend
from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.logging_config import get_logger, setup_logging
from kicad_mcp.resources.definitions import register_resources
from kicad_mcp.tools import board, drc, export, library, library_manage, project, routing, schematic
from kicad_mcp.utils.change_log import ChangeLog

logger = get_logger("server")


def create_server(config: KiCadMCPConfig | None = None) -> FastMCP:
    """Create and configure the KiCad MCP server.

    Args:
        config: Server configuration. Uses defaults/env vars if not provided.

    Returns:
        Configured FastMCP server instance ready to run.
    """
    if config is None:
        config = KiCadMCPConfig()

    # Setup logging
    setup_logging(
        level=config.log_level.value,
        log_file=config.get_log_file_path(),
    )
    logger.info("KiCad MCP Server v%s starting", __version__)

    # Create composite backend
    cli_path = str(config.kicad_cli_path) if config.kicad_cli_path else None
    backend = create_composite_backend(
        backend_type=config.backend,
        cli_path=cli_path,
    )

    # Create change log
    change_log = ChangeLog(config.get_change_log_path())

    # Create FastMCP server
    mcp = FastMCP(
        "KiCad MCP Server",
        version=__version__,
    )

    # Register all tools
    project.register_tools(mcp, backend, change_log)
    board.register_tools(mcp, backend, change_log)
    schematic.register_tools(mcp, backend, change_log)
    export.register_tools(mcp, backend, change_log)
    library.register_tools(mcp, backend, change_log)
    library_manage.register_tools(mcp, backend, change_log)
    drc.register_tools(mcp, backend, change_log)
    routing.register_tools(mcp, backend, change_log, config)

    # Register resources
    register_resources(mcp, backend)

    status = backend.get_status()
    logger.info(
        "Server ready: %d backends active, primary=%s",
        len(status["active_backends"]),
        status["primary_backend"],
    )

    return mcp
