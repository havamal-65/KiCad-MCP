"""Auto-surface the launcher when the stack starts a non-launcher way (F4).

The MCP server can be started without the launcher window — a Claude Code
session spawning the stdio server, or the hot-reload http dev server. KIR3
finding F4: the launcher (the product's status face) should surface itself
in those cases instead of staying invisible.

This module is the server side of that contract, so it must work in a
server-only install: stdlib-only, import-safe, and it never imports the
``launcher`` package. The launcher side holds a named mutex for its process
lifetime; the server probes that mutex and spawns the launcher only when it
is absent, the feature is enabled, and a launcher entry point exists.

Windows-only by design (the launcher is Windows-first); every probe is a
no-op elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("launcher_surface")

# One well-known name both sides agree on. Session-local namespace: the
# launcher window is a per-login-session singleton, not a machine-global one.
MUTEX_NAME = "Local\\kicad-mcp-launcher"

# Kill switch for CI / tests / headless contexts.
ENV_DISABLE = "KICAD_MCP_NO_AUTO_SURFACE"

_ERROR_ALREADY_EXISTS = 183


def acquire_launcher_mutex() -> Any | None:
    """Create and hold the launcher singleton mutex (launcher boot path).

    Returns an OS handle the caller must keep referenced for its process
    lifetime (the OS releases it on process exit), or None when the mutex is
    already held by another launcher or the platform has no mutex API.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if not handle:
            return None
        if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return None


def launcher_running() -> bool:
    """True when a launcher window currently holds the singleton mutex."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _settings_path() -> Path:
    """The launcher settings file, by shared convention (no launcher import)."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "kicad-mcp-launcher" / "settings.json"


def auto_surface_enabled() -> bool:
    """Feature gate: env kill-switch wins; else the launcher settings'
    ``auto_surface`` key; default True (absent/corrupt file enables)."""
    if os.environ.get(ENV_DISABLE, "").strip() not in ("", "0"):
        return False
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    value = data.get("auto_surface") if isinstance(data, dict) else None
    return True if not isinstance(value, bool) else value


def _repo_root() -> Path:
    # src/kicad_mcp/utils/launcher_surface.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def _spawn_detached(cmd: list[str], cwd: Path | None) -> bool:
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        logger.debug("launcher spawn failed: %s", exc)
        return False


def spawn_launcher() -> bool:
    """Best-effort launcher spawn: frozen sibling exe, else repo venv dev path.

    Returns False when no launcher entry point exists (server-only install)
    or the spawn fails — never raises.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).parent / "KiCad-MCP Launcher.exe"
        if exe.exists():
            return _spawn_detached([str(exe)], exe.parent)
        return False
    root = _repo_root()
    if not (root / "launcher" / "__main__.py").exists():
        return False
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        return False
    return _spawn_detached([str(pythonw), "-m", "launcher"], root)


def maybe_surface_launcher() -> str:
    """Surface the launcher if appropriate; returns the outcome for logging.

    Outcomes: ``off_platform`` (not Windows), ``disabled`` (env/settings),
    ``already_running`` (mutex held), ``spawned``, ``unavailable`` (no entry
    point or spawn failed). Never raises.
    """
    try:
        if sys.platform != "win32":
            return "off_platform"
        if not auto_surface_enabled():
            return "disabled"
        if launcher_running():
            return "already_running"
        return "spawned" if spawn_launcher() else "unavailable"
    except Exception as exc:  # noqa: BLE001 — surfacing is never allowed to break the server
        logger.debug("maybe_surface_launcher: %s", exc)
        return "unavailable"
