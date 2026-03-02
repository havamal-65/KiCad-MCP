"""FastMCP server creation and tool/resource registration."""

from __future__ import annotations

import functools
import json

from fastmcp import FastMCP

from kicad_mcp import __version__
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.backends.factory import create_composite_backend
from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.logging_config import get_logger, setup_logging
from kicad_mcp.resources.definitions import register_resources
from kicad_mcp.tools import board, drc, export, library, library_manage, project, routing, schematic
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.platform_helper import is_kicad_running, launch_kicad

logger = get_logger("server")

_KICAD_NOT_RUNNING_RESPONSE = json.dumps({
    "status": "error",
    "error": (
        "KiCad GUI is not running. "
        "Use the open_kicad tool to launch KiCad first, then retry."
    ),
}, indent=2)


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

    # Register open_kicad first — exempt from the KiCad-running guard because
    # it is the tool used to START KiCad when it isn't running.
    @mcp.tool()
    def open_kicad(project_path: str = "") -> str:
        """Open the KiCad GUI application.

        Checks if KiCad is already running. If not, launches it automatically.
        Optionally opens a specific project file on launch.

        Args:
            project_path: Optional path to a .kicad_pro file to open.

        Returns:
            JSON with status and message.
        """
        if is_kicad_running():
            return json.dumps({"status": "success", "message": "KiCad is already running."}, indent=2)

        from pathlib import Path
        p: Path | None = None
        if project_path:
            p = Path(project_path)
            if not p.exists():
                return json.dumps({"status": "error", "error": f"Project file not found: {project_path}"}, indent=2)

        launched = launch_kicad(p)
        if launched:
            return json.dumps({
                "status": "success",
                "message": "KiCad launched. It may take a few seconds to fully open.",
            }, indent=2)
        return json.dumps({
            "status": "error",
            "error": "Failed to launch KiCad. Verify it is installed.",
        }, indent=2)

    # Wrap mcp.tool so every subsequently registered tool checks that KiCad is
    # running before executing.  open_kicad (above) is already registered and
    # is unaffected.
    _original_tool = mcp.tool

    def _guarded_tool(*args, **kwargs):
        decorator = _original_tool(*args, **kwargs)

        def wrapper(func):
            @functools.wraps(func)
            def guarded(*fargs, **fkwargs):
                if not is_kicad_running():
                    return _KICAD_NOT_RUNNING_RESPONSE
                return func(*fargs, **fkwargs)
            return decorator(guarded)

        return wrapper

    mcp.tool = _guarded_tool  # type: ignore[method-assign]

    # Register all tools (each will be wrapped by _guarded_tool)
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
