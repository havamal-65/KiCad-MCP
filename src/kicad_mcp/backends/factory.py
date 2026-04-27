"""Backend auto-detection and factory."""

from __future__ import annotations

from kicad_mcp.backends.base import KiCadBackend
from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.config import BackendType
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import BackendNotAvailableError

logger = get_logger("backend.factory")


def create_composite_backend(
    backend_type: BackendType = BackendType.AUTO,
    cli_path: str | None = None,
) -> CompositeBackend:
    """Create a CompositeBackend with available backends based on configuration.

    Args:
        backend_type: Which backend(s) to use. AUTO detects all available.
        cli_path: Optional explicit path to kicad-cli.

    Returns:
        CompositeBackend routing operations to available backends.

    Raises:
        BackendNotAvailableError: If the explicitly requested backend is not available.
    """
    backends: list[KiCadBackend] = []

    if backend_type == BackendType.AUTO:
        backends, plugin_watchdog = _auto_detect_backends(cli_path)
    elif backend_type == BackendType.IPC:
        backend = _try_ipc()
        if backend is None:
            raise BackendNotAvailableError(
                "IPC backend not available. Requires KiCad 9+ running and kicad-python installed."
            )
        backends.append(backend)
        # Add file backend as fallback for schematic ops
        backends.append(_create_file_backend())
    elif backend_type == BackendType.SWIG:
        backend = _try_swig()
        if backend is None:
            raise BackendNotAvailableError(
                "SWIG backend not available. Requires KiCad 7-8 pcbnew Python bindings."
            )
        backends.append(backend)
        backends.append(_create_file_backend())
    elif backend_type == BackendType.CLI:
        backend = _try_cli(cli_path)
        if backend is None:
            raise BackendNotAvailableError(
                "CLI backend not available. kicad-cli not found."
            )
        backends.append(backend)
        backends.append(_create_file_backend())
    elif backend_type == BackendType.FILE:
        backends.append(_create_file_backend())

    if not backends:
        logger.warning("No backends detected, using file-only mode")
        backends.append(_create_file_backend())

    logger.info(
        "Initialized backends: %s",
        [b.name for b in backends],
    )
    if backend_type == BackendType.AUTO:
        return CompositeBackend(backends, plugin_watchdog=plugin_watchdog)
    return CompositeBackend(backends)


def _auto_detect_backends(
    cli_path: str | None = None,
) -> tuple[list[KiCadBackend], KiCadBackend | None]:
    """Detect all available backends in priority order.

    Returns:
        (backends, plugin_watchdog) where plugin_watchdog is a PluginBackend
        instance kept as a watchdog for dynamic discovery even if not yet live.
    """
    backends: list[KiCadBackend] = []
    plugin_watchdog: KiCadBackend | None = None

    # Try plugin (direct pcbnew access via in-KiCad bridge — most accurate)
    try:
        from kicad_mcp.backends.plugin_backend import PluginBackend
        pb = PluginBackend()
        plugin_watchdog = pb  # keep as watchdog regardless of current availability
        if pb.is_available():
            backends.append(pb)
            logger.info("Plugin backend available (kicad_mcp_bridge running in KiCad)")
    except Exception as e:
        logger.debug("Plugin backend not importable: %s", e)

    # Try IPC (KiCad 9+)
    ipc = _try_ipc()
    if ipc:
        backends.append(ipc)
        logger.info("IPC backend available (KiCad 9+)")

    # Try SWIG (KiCad 7-8)
    swig = _try_swig()
    if swig:
        backends.append(swig)
        logger.info("SWIG backend available")

    # Try subprocess pcbnew (DSN/SES routing when plugin bridge is not active)
    subprocess_b = _try_subprocess()
    if subprocess_b:
        backends.append(subprocess_b)
        logger.info("Subprocess backend available (pcbnew subprocess for DSN/SES)")

    # Try CLI
    cli = _try_cli(cli_path)
    if cli:
        backends.append(cli)
        logger.info("CLI backend available")

    # File backend always available
    backends.append(_create_file_backend())
    logger.info("File backend available (always)")

    return backends, plugin_watchdog


def _try_plugin() -> KiCadBackend | None:
    """Try to create a Plugin backend (kicad_mcp_bridge TCP server)."""
    try:
        from kicad_mcp.backends.plugin_backend import PluginBackend
        backend = PluginBackend()
        if backend.is_available():
            return backend
    except Exception as e:
        logger.debug("Plugin backend not available: %s", e)
    return None


def _try_subprocess() -> KiCadBackend | None:
    """Try to create a Subprocess backend (pcbnew via KiCad Python for DSN/SES ops)."""
    try:
        from kicad_mcp.backends.subprocess_backend import SubprocessBackend
        backend = SubprocessBackend()
        if backend.is_available():
            return backend
    except Exception as e:
        logger.debug("Subprocess backend not available: %s", e)
    return None


def _try_ipc() -> KiCadBackend | None:
    """Try to create an IPC backend."""
    try:
        from kicad_mcp.backends.ipc_backend import IPCBackend
        backend = IPCBackend()
        if backend.is_available():
            return backend
    except Exception as e:
        logger.debug("IPC backend not available: %s", e)
    return None


def _try_swig() -> KiCadBackend | None:
    """Try to create a SWIG backend."""
    try:
        from kicad_mcp.backends.swig_backend import SWIGBackend
        backend = SWIGBackend()
        if backend.is_available():
            return backend
    except Exception as e:
        logger.debug("SWIG backend not available: %s", e)
    return None


def _try_cli(cli_path: str | None = None) -> KiCadBackend | None:
    """Try to create a CLI backend."""
    try:
        from pathlib import Path

        from kicad_mcp.backends.cli_backend import CLIBackend
        path = Path(cli_path) if cli_path else None
        backend = CLIBackend(cli_path=path)
        if backend.is_available():
            return backend
    except Exception as e:
        logger.debug("CLI backend not available: %s", e)
    return None


def _create_file_backend() -> KiCadBackend:
    """Create the file-parsing backend (always available)."""
    from kicad_mcp.backends.file_backend import FileBackend
    return FileBackend()


def get_available_backends() -> dict[str, dict]:
    """Check which backends are available. Useful for diagnostics."""
    results = {}

    for name, try_fn in [
        ("plugin", _try_plugin),
        ("ipc", _try_ipc),
        ("swig", _try_swig),
        ("cli", lambda: _try_cli()),
    ]:
        backend = try_fn()
        results[name] = {
            "available": backend is not None,
            "version": backend.get_version() if backend else None,
        }

    results["file"] = {"available": True, "version": None}
    return results
