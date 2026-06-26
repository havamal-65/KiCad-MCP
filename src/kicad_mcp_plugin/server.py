"""FastMCP server creation for the plugin entry point."""

from __future__ import annotations

import functools
import json
import os
import time

from fastmcp import FastMCP

from kicad_mcp import __version__
from kicad_mcp.backends.plugin_backend import _get_ping_timeout, _get_port, _tcp_call  # noqa: PLC2701
from kicad_mcp.logging_config import get_logger, setup_logging
from kicad_mcp.resources.definitions import register_resources
from kicad_mcp.tools import (
    board,
    drc,
    export,
    library,
    library_manage,
    manufacturing,
    parts,
    project,
    routing,
    schematic,
)
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.platform_helper import (
    cleanup_stale_session_files,
    is_kicad_running,
    is_pcbnew_running,
    launch_kicad,
    launch_pcbnew,
    wait_for_bridge,
)
from kicad_mcp_plugin.backends.plugin_direct import BridgeNotAvailableError, PluginDirectBackend
from kicad_mcp_plugin.config import KiCadPluginConfig

logger = get_logger("plugin.server")


def _wait_for_board(pcb_path: "Path", timeout: float = 10.0, interval: float = 0.5) -> bool:
    """Poll get_active_project until board_path matches pcb_path or timeout expires.

    The bridge answers ping as soon as the TCP port is bound, but pcbnew loads
    the board file asynchronously.  This helper waits until the bridge reports
    the correct board_path before callers assume the board is ready.
    """
    from pathlib import Path  # noqa: PLC0415 — avoid circular at module level

    requested = os.path.normcase(os.path.normpath(str(pcb_path)))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            active = _tcp_call("get_active_project", _get_ping_timeout())
            open_board = active.get("board_path", "") if isinstance(active, dict) else ""
            if open_board and os.path.normcase(os.path.normpath(open_board)) == requested:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _norm_path(p: str) -> str:
    """Case/normalize a path for cross-platform comparison."""
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _evaluate_launch_guard(
    pcb_path: "Path",
    identity: dict | None,
    pcbnew_running: bool,
    force: bool,
) -> dict | None:
    """Decide whether open_kicad should launch pcbnew for *pcb_path* (#13A).

    Pure decision function (no I/O) so it is unit-testable.  The caller fetches
    the current state — the bridge ping *identity* (None if unreachable or
    wrong-owner), whether a pcbnew editor is already running, and the user's
    *force* flag — and this returns either:

      * a terminal response dict (reuse-success or a structured refusal), or
      * None, meaning "safe to launch pcbnew".

    The refusals exist to stop the issue-#13 double-launch: launching a second
    pcbnew while one is already open leaks the bridge port and can trigger
    autosave reverts.  ``force=True`` overrides every refusal.
    """
    bridge_up = isinstance(identity, dict) and identity.get("pong") is True
    # A bridge that identifies as a non-pcbnew owner (e.g. the project manager
    # holding the port) is not a usable editor session.
    wrong_owner = bridge_up and identity.get("app") not in (None, "pcbnew")

    if bridge_up and not wrong_owner:
        open_board = identity.get("board_path") or ""
        if open_board and _norm_path(open_board) == _norm_path(str(pcb_path)):
            return {
                "status": "success",
                "bridge": "ready",
                "message": f"Reusing the existing KiCad session — {pcb_path.name} is already open.",
            }
        # Bridge is a live pcbnew editor on a DIFFERENT (or unknown) board.
        if not force:
            return {
                "status": "refused",
                "bridge": "ready",
                "open_board": open_board or None,
                "requested_board": str(pcb_path),
                "error": (
                    f"KiCad already has a different board open ("
                    f"{open_board or 'unknown'}). Launching pcbnew for "
                    f"{pcb_path.name} would start a SECOND editor instance and leak "
                    "the bridge port (issue #13). Close the current board and open "
                    f"{pcb_path.name} in pcbnew, or call open_kicad again with "
                    "force=True to launch anyway."
                ),
            }
        return None  # force → launch

    # No usable bridge.  If a pcbnew editor is already running (bridge down or
    # wrong-owner), launching another would double up — refuse unless forced.
    if pcbnew_running and not force:
        return {
            "status": "refused",
            "bridge": "down",
            "requested_board": str(pcb_path),
            "error": (
                "A pcbnew editor is already running but its bridge is not "
                "reachable (it may still be loading, the bridge plugin may be "
                "disabled, or the project manager is holding the port). Opening "
                f"{pcb_path.name} now risks a second instance and a leaked bridge "
                "port (issue #13). Close all KiCad windows and retry, or call "
                "open_kicad again with force=True to launch anyway."
            ),
        }

    return None  # safe to launch (nothing running, or force)


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
    def open_kicad(project_path: str = "", force: bool = False) -> str:
        """Open KiCad and the PCB editor, then wait for the MCP bridge to start.

        Handles the full startup sequence automatically:
        1. Resolves the PCB file from the supplied path (.kicad_pcb or .kicad_pro).
        2. Launches pcbnew with that file so the kicad_mcp_bridge plugin loads.
        3. Waits up to 20 seconds for the bridge TCP port to become available.

        If no path is supplied and KiCad is already running with the bridge active,
        reports success immediately.  If no path is supplied and the bridge is not
        reachable, launches the KiCad project manager and asks the user to open a
        board in the PCB editor.

        To avoid a second pcbnew instance leaking the bridge port (issue #13),
        the tool refuses to launch when a different board is already open or a
        pcbnew editor is running without a reachable bridge.  Pass force=True to
        override that guard.

        Args:
            project_path: Path to a .kicad_pcb or .kicad_pro file to open.
                          Leave empty to launch the KiCad project manager only.
            force: Launch pcbnew even when the double-launch guard would refuse.

        Returns:
            JSON with status and bridge availability.
        """
        from pathlib import Path

        port = _get_port()

        # [DIAG] Log every call so we can confirm this code version is executing
        # and verify what project_path value arrives.
        logger.info("open_kicad called: project_path=%r force=%s", project_path, force)

        # --- check if bridge is already up (capture full identity, #13B) ---
        identity: dict | None = None
        try:
            result = _tcp_call("ping", _get_ping_timeout())
            if isinstance(result, dict) and result.get("pong") is True:
                identity = result
        except Exception:
            pass  # bridge not yet up — continue with launch sequence

        # A bridge that identifies as a non-pcbnew owner (project manager holding
        # the port) is not a usable editor session.
        wrong_owner = identity is not None and identity.get("app") not in (None, "pcbnew")
        bridge_is_up = identity is not None and not wrong_owner

        logger.info("open_kicad: bridge_is_up=%s wrong_owner=%s", bridge_is_up, wrong_owner)

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

        # --- double-launch guard (#13A) ---
        if pcb_path is not None:
            # Legacy bridges report no board_path in ping; backfill it so the
            # guard can recognize a same-board reuse against an old bridge.
            if bridge_is_up and not (identity or {}).get("board_path"):
                try:
                    active = _tcp_call("get_active_project", _get_ping_timeout())
                    if isinstance(active, dict) and active.get("board_path"):
                        identity = {**(identity or {}), "board_path": active["board_path"]}
                except Exception as exc:
                    logger.info("open_kicad: get_active_project backfill failed: %s", exc)

            guard = _evaluate_launch_guard(
                pcb_path, identity, is_pcbnew_running(), force,
            )
            if guard is not None:
                logger.info("open_kicad: guard decision status=%s", guard.get("status"))
                return json.dumps(guard, indent=2)

        # --- launch pcbnew (preferred) or project manager ---
        if pcb_path is not None:
            # Remove stale lock/autosave files so the relaunch opens the on-disk
            # board, not a stale autosave (#14B).  No-op while KiCad is running.
            stale_removed = cleanup_stale_session_files(pcb_path.parent)
            if stale_removed:
                logger.info("open_kicad: removed stale session files: %s", stale_removed)

            launched = launch_pcbnew(pcb_path)
            if not launched:
                return json.dumps({
                    "status": "error",
                    "error": "Failed to launch pcbnew. Verify KiCad is installed.",
                }, indent=2)

            def _launch_response(bridge: str, message: str) -> str:
                payload: dict = {"status": "success", "bridge": bridge, "message": message}
                if stale_removed:
                    payload["stale_files_removed"] = stale_removed
                return json.dumps(payload, indent=2)

            bridge_up = wait_for_bridge(port=port, timeout=20.0)
            if bridge_up:
                board_ready = _wait_for_board(pcb_path, timeout=10.0)
                if board_ready:
                    return _launch_response(
                        "ready",
                        f"pcbnew opened with {pcb_path.name} and bridge is ready.",
                    )
                # Bridge is up but pcbnew hasn't finished loading the board yet.
                return _launch_response(
                    "pending",
                    f"pcbnew launched with {pcb_path.name}. Bridge is reachable but "
                    "the board has not finished loading yet. Retry in a few seconds.",
                )
            return _launch_response(
                "pending",
                f"pcbnew launched with {pcb_path.name} but bridge not reachable yet. "
                "Ensure kicad_mcp_bridge is installed and enabled in KiCad, then retry.",
            )

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
    parts.register_tools(mcp, backend, change_log)
    drc.register_tools(mcp, backend, change_log)
    export.register_tools(mcp, backend, change_log)
    # §6.7 manufacturing-readiness audit — pure orchestration over the file-side
    # checks + the same CLI export/DRC ops as export.py; no bridge required.
    manufacturing.register_tools(mcp, backend, change_log)

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
