"""FastMCP server creation for the plugin entry point."""

from __future__ import annotations

import functools
import json
import os

from fastmcp import FastMCP

from kicad_mcp import __version__
from kicad_mcp.backends.plugin_backend import _get_ping_timeout, _get_port, _tcp_call  # noqa: PLC2701
from kicad_mcp.logging_config import get_logger, setup_logging
from kicad_mcp.resources.definitions import register_resources
from kicad_mcp.tools import board, drc, export, library, library_manage, project, routing, schematic
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.platform_helper import (
    is_kicad_running,
    launch_kicad,
    launch_pcbnew,
    wait_for_bridge,
)
from kicad_mcp_plugin.backends.plugin_direct import BridgeNotAvailableError, PluginDirectBackend
from kicad_mcp_plugin.config import KiCadPluginConfig

logger = get_logger("plugin.server")

_BRIDGE_DOWN_RESPONSE = json.dumps({
    "status": "error",
    "error": (
        "kicad_mcp_bridge is not reachable. "
        "Open KiCad, ensure kicad_mcp_bridge is installed and enabled, then retry."
    ),
}, indent=2)


def create_plugin_server(config: KiCadPluginConfig | None = None) -> FastMCP:
    """Create and configure the KiCad MCP Plugin server.

    The server always starts — even if kicad_mcp_bridge is not yet reachable.
    open_kicad is registered unguarded so KiCad can be launched from a cold state.
    All other tools re-probe the bridge on every call and fail loudly (never
    silently fall back to the file backend) if the bridge is down.

    Args:
        config: Server configuration. Uses defaults/env vars if not provided.

    Returns:
        Configured FastMCP server instance ready to run.
    """
    if config is None:
        config = KiCadPluginConfig()

    setup_logging(
        level=config.log_level.value,
        log_file=config.get_log_file_path(),
    )
    logger.info("KiCad MCP Plugin Server v%s starting", __version__)

    cli_path = str(config.kicad_cli_path) if config.kicad_cli_path else None
    backend = PluginDirectBackend(cli_path=cli_path)

    change_log = ChangeLog(config.get_change_log_path())

    mcp = FastMCP(
        "KiCad MCP Plugin Server",
        version=__version__,
    )

    # Register open_kicad first — exempt from the bridge guard because it is
    # the tool used to START KiCad when it isn't running yet.
    @mcp.tool()
    def open_kicad(project_path: str = "") -> str:
        """Open KiCad and the PCB editor, then wait for the MCP bridge to start.

        Handles the full startup sequence automatically:
        1. Resolves the PCB file from the supplied path (.kicad_pcb or .kicad_pro).
        2. Launches pcbnew with that file so the kicad_mcp_bridge plugin loads.
        3. Waits up to 20 seconds for the bridge TCP port to become available.

        If no path is supplied and KiCad is already running with the bridge active,
        reports success immediately.  If no path is supplied and the bridge is not
        reachable, launches the KiCad project manager and asks the user to open a
        board in the PCB editor.

        Args:
            project_path: Path to a .kicad_pcb or .kicad_pro file to open.
                          Leave empty to launch the KiCad project manager only.

        Returns:
            JSON with status and bridge availability.
        """
        from pathlib import Path

        port = _get_port()

        # [DIAG] Log every call so we can confirm this code version is executing
        # and verify what project_path value arrives.
        logger.info("open_kicad called: project_path=%r", project_path)

        # --- check if bridge is already up ---
        bridge_is_up = False
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            bridge_is_up = isinstance(result, dict) and result.get("pong") is True
        except Exception:
            pass  # bridge not yet up — continue with launch sequence

        logger.info("open_kicad: bridge_is_up=%s", bridge_is_up)

        # If bridge is running and no specific board was requested, we're done.
        if bridge_is_up and not project_path:
            logger.info("open_kicad: no project_path and bridge up — returning ready immediately")
            return json.dumps({
                "status": "success",
                "bridge": "ready",
                "message": "KiCad bridge is already running.",
            }, indent=2)

        # --- resolve PCB file ---
        pcb_path: Path | None = None
        if project_path:
            p = Path(project_path)
            if not p.exists():
                logger.info("open_kicad: path does not exist: %r", project_path)
                return json.dumps(
                    {"status": "error", "error": f"File not found: {project_path}"},
                    indent=2,
                )
            if p.suffix == ".kicad_pcb":
                pcb_path = p
            elif p.suffix == ".kicad_pro":
                candidate = p.with_suffix(".kicad_pcb")
                if candidate.exists():
                    pcb_path = candidate
                else:
                    # fall back to any .kicad_pcb in the same directory
                    pcbs = list(p.parent.glob("*.kicad_pcb"))
                    if pcbs:
                        pcb_path = pcbs[0]
            else:
                return json.dumps(
                    {"status": "error", "error": "project_path must be a .kicad_pcb or .kicad_pro file"},
                    indent=2,
                )

        logger.info("open_kicad: resolved pcb_path=%r", str(pcb_path) if pcb_path else None)

        # If bridge is up and a specific board was requested, check whether that
        # board is already the one open in KiCad.  If yes, return immediately.
        # If no, fall through to launch pcbnew with the correct path.
        if bridge_is_up and pcb_path is not None:
            open_board = ""
            try:
                active = _tcp_call("get_active_project", _get_ping_timeout())
                open_board = active.get("board_path", "") if isinstance(active, dict) else ""
                logger.info("open_kicad: get_active_project returned board_path=%r", open_board)
            except Exception as exc:
                logger.info("open_kicad: get_active_project failed: %s", exc)

            requested_norm = os.path.normcase(os.path.normpath(str(pcb_path)))
            open_norm = os.path.normcase(os.path.normpath(open_board)) if open_board else ""
            logger.info(
                "open_kicad: comparing open=%r vs requested=%r — match=%s",
                open_norm, requested_norm, open_norm == requested_norm,
            )

            if open_board and open_norm == requested_norm:
                return json.dumps({
                    "status": "success",
                    "bridge": "ready",
                    "message": f"KiCad bridge is ready with {pcb_path.name} open.",
                }, indent=2)

            # Board doesn't match — fall through to launch pcbnew with the new path.
            logger.info(
                "open_kicad: board mismatch — re-launching pcbnew with %r (was %r)",
                str(pcb_path), open_board,
            )

        # --- launch pcbnew (preferred) or project manager ---
        if pcb_path is not None:
            launched = launch_pcbnew(pcb_path)
            if not launched:
                return json.dumps({
                    "status": "error",
                    "error": "Failed to launch pcbnew. Verify KiCad is installed.",
                }, indent=2)

            bridge_up = wait_for_bridge(port=port, timeout=20.0)
            if bridge_up:
                return json.dumps({
                    "status": "success",
                    "bridge": "ready",
                    "message": f"pcbnew opened with {pcb_path.name} and bridge is ready.",
                }, indent=2)
            return json.dumps({
                "status": "success",
                "bridge": "pending",
                "message": (
                    f"pcbnew launched with {pcb_path.name} but bridge not reachable yet. "
                    "Ensure kicad_mcp_bridge is installed and enabled in KiCad, then retry."
                ),
            }, indent=2)

        # --- no PCB file: fall back to project manager ---
        if not is_kicad_running():
            launched = launch_kicad()
            if not launched:
                return json.dumps({
                    "status": "error",
                    "error": "Failed to launch KiCad. Verify it is installed.",
                }, indent=2)

        return json.dumps({
            "status": "success",
            "bridge": "pending",
            "message": (
                "KiCad is running but the bridge needs the PCB editor open. "
                "Call open_kicad again with a project_path pointing to a "
                ".kicad_pcb or .kicad_pro file to complete startup."
            ),
        }, indent=2)

    # Register tools that work without the bridge (file backend / CLI / pure file I/O).
    # These must be registered BEFORE the guard so they function from a cold start —
    # e.g. create_project must succeed before any .kicad_pcb exists to open in KiCad.
    project.register_tools(mcp, backend, change_log)
    schematic.register_tools(mcp, backend, change_log)
    library.register_tools(mcp, backend, change_log)
    library_manage.register_tools(mcp, backend, change_log)
    drc.register_tools(mcp, backend, change_log)
    export.register_tools(mcp, backend, change_log)

    # Install bridge guard for board and routing tools — the only two modules whose
    # every tool requires BOARD_READ / BOARD_MODIFY / ZONE_REFILL / BOARD_STACKUP,
    # all of which route exclusively to the plugin backend (pcbnew must be open).
    # There is no silent fallback to any other backend for these tools.
    _original_tool = mcp.tool

    def _guarded_tool(*args, **kwargs):
        decorator = _original_tool(*args, **kwargs)

        def wrapper(func):
            @functools.wraps(func)
            def guarded(*fargs, **fkwargs):
                try:
                    result = _tcp_call("ping", _get_ping_timeout())
                    if not isinstance(result, dict) or result.get("pong") is not True:
                        raise BridgeNotAvailableError("pong check failed")
                except (BridgeNotAvailableError, ConnectionRefusedError, OSError, TimeoutError):
                    return _BRIDGE_DOWN_RESPONSE
                return func(*fargs, **fkwargs)
            return decorator(guarded)

        return wrapper

    mcp.tool = _guarded_tool  # type: ignore[method-assign]

    board.register_tools(mcp, backend, change_log)
    routing.register_tools(mcp, backend, change_log, config)

    register_resources(mcp, backend)

    if backend._bridge_available:
        logger.info("Plugin server ready: bridge at localhost:%d", _get_port())
    else:
        logger.warning(
            "Plugin server started without bridge. "
            "Use open_kicad to launch KiCad, then enable kicad_mcp_bridge."
        )
    return mcp
