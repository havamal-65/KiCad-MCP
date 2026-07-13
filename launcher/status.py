"""Live status adapter — reuses the existing health monitor's collectors.

The launcher's status panel does NOT reimplement health collection; it lazily
loads `scripts/mcp_health_monitor.py` and calls its `collect()` (REQ-STATUS-002).
The monitor is never imported at module load (it imports `tkinter` at its top),
so this module stays import-safe on headless CI — the load happens only at the
first status poll, on Windows. If the monitor can't be loaded or `collect()`
raises, a degraded snapshot is returned instead of an exception
(REQ-STATUS-003). The monitor file is never modified (AR1).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
_MONITOR_PATH = REPO_ROOT / "scripts" / "mcp_health_monitor.py"

_monitor_mod: Any | None = None
_monitor_load_failed = False


def _load_monitor() -> Any | None:
    """Lazily import the health monitor module by file path. Cached."""
    global _monitor_mod, _monitor_load_failed
    if _monitor_mod is not None:
        return _monitor_mod
    if _monitor_load_failed:
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "kicad_mcp_health_monitor", _MONITOR_PATH
        )
        if spec is None or spec.loader is None:
            _monitor_load_failed = True
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _monitor_mod = mod
        return mod
    except Exception:
        _monitor_load_failed = True
        return None


def _degraded(reason: str) -> dict[str, Any]:
    """Minimal snapshot without the monitor — uses only platform_helper."""
    try:
        from kicad_mcp.utils import platform_helper

        pcbnew = platform_helper.is_pcbnew_running()
    except Exception:
        pcbnew = None
    return {
        "degraded": True,
        "reason": reason,
        "pcbnew_running": pcbnew,
    }


def get_status() -> dict[str, Any]:
    """Live status by reusing the monitor's `collect()`. Never raises."""
    mod = _load_monitor()
    if mod is None:
        return _degraded(f"health monitor unavailable ({_MONITOR_PATH.name})")
    collect = getattr(mod, "collect", None)
    if collect is None:
        return _degraded("health monitor has no collect()")
    try:
        result: dict[str, Any] = collect()
        return result
    except Exception as exc:
        return _degraded(f"{type(exc).__name__}: {exc}")
