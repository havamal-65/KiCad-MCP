"""Env-gated auto-start of the MCP health-monitor UI.

The MCP server calls :func:`maybe_launch_health_ui` once at startup. When the
``KICAD_MCP_HEALTH_UI`` environment variable is set to a truthy value, the
server best-effort spawns the existing ``scripts/mcp_health_monitor.py`` window
so the health UI "runs with the MCP" (Stack Launcher REQ-AUTOSTART-*). When the
variable is unset the call is a no-op, so the default end-user stdio path is
unchanged.

Import-safe on every platform: this module imports only the stdlib at load time
(no ``tkinter``); ``psutil`` is imported lazily and its absence is tolerated.
The health monitor script it launches is never imported here — it is spawned as
a separate detached process — and is never modified.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Substring that identifies an already-running health-monitor process by its
# command line. Matches scripts/mcp_health_monitor.py regardless of how it was
# launched (pythonw, python, or the .ps1 wrapper).
_MONITOR_MARKER = "mcp_health_monitor"

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _repo_root() -> Path:
    # src/kicad_mcp/utils/health_ui.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def _venv_pythonw() -> Path:
    """Prefer venv pythonw (no console box), fall back to venv python, then
    the current interpreter."""
    root = _repo_root()
    if sys.platform == "win32":
        candidates = [
            root / ".venv" / "Scripts" / "pythonw.exe",
            root / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [root / ".venv" / "bin" / "python"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _health_ui_already_running() -> bool:
    """True if a health-monitor process is already up.

    Uses psutil when available; if psutil is absent we cannot tell, so we
    return False (spawn once) rather than suppress the launch.
    """
    try:
        import psutil
    except Exception:
        return False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if any(_MONITOR_MARKER in str(part) for part in cmd):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return False


def maybe_launch_health_ui() -> None:
    """Env-gated, singleton-guarded, best-effort launch of the health UI.

    Never raises and never blocks server startup. No-op unless
    ``KICAD_MCP_HEALTH_UI`` is truthy.
    """
    if not _truthy(os.environ.get("KICAD_MCP_HEALTH_UI")):
        return  # default: no-op, stdio path unchanged (REQ-AUTOSTART-003)
    try:
        if _health_ui_already_running():
            logger.debug("health UI already running — not launching a second window")
            return
        pyw = _venv_pythonw()
        script = _repo_root() / "scripts" / "mcp_health_monitor.py"
        if not script.exists():
            logger.warning("health UI auto-start skipped: %s not found", script)
            return
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        subprocess.Popen([str(pyw), str(script)], creationflags=creationflags)
        logger.info("health UI auto-started (%s)", pyw)
    except Exception as exc:
        # Best-effort: a failed UI launch must never take down the server.
        logger.warning("health UI auto-start failed (non-fatal): %s", exc)
