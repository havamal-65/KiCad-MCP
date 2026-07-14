"""Dashboard state builder — pure, import-safe (no GUI, no tkinter/webview).

Reshapes a health-monitor `collect()` snapshot into the state shape the webview
UI renders (mirrors the design's state machine, but fed by REAL data — no mock
timers, no fabricated activity). `build_state` is a pure function over a
snapshot dict + the picker list, so it unit-tests without KiCad or a window.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# The 6 authoritative startup checks, in display order (same keys the monitor's
# read_checklist() emits and the design expects).
CHECK_ORDER = [
    "kicad_running",
    "bridge_reachable",
    "bridge_version_ok",
    "pcb_editor_open",
    "kicad_cli_available",
    "project_loaded",
]

# Monitor backend row name -> design backend key.
_BACKEND_KEY = {
    "plugin (bridge)": "plugin",
    "plugin": "plugin",
    "file": "file",
    "cli": "cli",
    "subprocess": "subprocess",
}

BRIDGE_PORT = 9760


def _avail(value: Any) -> str:
    if value is True:
        return "up"
    if value is False:
        return "down"
    return "idle"  # None / unknown


def _hhmmss(ts: Any, epoch: Any = None) -> str:
    if isinstance(epoch, (int, float)) and epoch > 0:
        try:
            return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
        except (ValueError, OSError, OverflowError):
            pass
    if isinstance(ts, str) and ts:
        # ISO or already HH:MM:SS — take the time portion if present.
        t = ts.replace("T", " ")
        if " " in t:
            t = t.split(" ", 1)[1]
        return t[:8]
    return "--:--:--"


def _checklist(snapshot: dict[str, Any]) -> dict[str, bool]:
    checks = (snapshot.get("checklist") or {}).get("checks") or []
    by_item = {c.get("item"): (c.get("status") == "PASS") for c in checks}
    return {k: bool(by_item.get(k, False)) for k in CHECK_ORDER}


def _backends(snapshot: dict[str, Any]) -> dict[str, str]:
    rows = (snapshot.get("backends") or {}).get("rows") or []
    out = {"plugin": "down", "file": "up", "cli": "idle", "subprocess": "idle"}
    for r in rows:
        key = _BACKEND_KEY.get(r.get("name", ""))
        if key:
            out[key] = _avail(r.get("available"))
    return out


def _bridge(snapshot: dict[str, Any], busy_phase: str | None) -> dict[str, Any]:
    b = snapshot.get("bridge") or {}
    board = snapshot.get("board") or {}
    state = b.get("state")
    open_board = board.get("open_board")
    if busy_phase in ("starting", "restarting"):
        status = "starting"
        detail = "connecting…"
    elif state == "HEALTHY" and board.get("bridge_access") in (None, "ok"):
        status = "running"
        detail = f"connected · TCP :{BRIDGE_PORT}"
    elif state == "HEALTHY":
        status = "starting"  # pongs but board access impaired
        detail = "connected · board access impaired"
    else:
        status = "unresponsive"
        detail = b.get("detail") or (state.lower() if isinstance(state, str) else "not connected")
    return {"status": status, "detail": detail, "board": open_board or "none open"}


def _server(snapshot: dict[str, Any]) -> dict[str, Any]:
    s = snapshot.get("server") or {}
    running = bool(s.get("running"))
    return {
        "status": "running" if running else "stopped",
        "pid": s.get("pid"),
        "uptimeSeconds": s.get("uptime"),
    }


def _activity(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    recent = (snapshot.get("activity") or {}).get("recent") or []
    out = []
    for a in recent[:7]:
        out.append(
            {
                "time": _hhmmss(a.get("ts"), a.get("epoch")),
                "tool": a.get("tool", "?"),
                "status": a.get("status", "ok"),
            }
        )
    return out


def _logs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    events = (snapshot.get("errors") or {}).get("events") or []
    out = []
    for e in events[-4:][::-1]:  # newest first, cap 4
        out.append(
            {
                "time": _hhmmss(e.get("ts")),
                "level": e.get("level", "INFO"),
                "msg": e.get("msg", ""),
            }
        )
    return out


def _phase(
    snapshot: dict[str, Any], checklist: dict[str, bool], bridge: dict[str, Any], busy_phase: str | None
) -> str:
    if busy_phase in ("starting", "restarting"):
        return busy_phase
    if not (snapshot.get("server") or {}).get("running"):
        return "stopped"
    ready = all(checklist.values())
    if bridge["status"] == "running" and ready:
        return "running"
    return "idle"


def build_state(
    snapshot: dict[str, Any] | None,
    projects: list[Any],
    selected_index: int,
    busy_phase: str | None = None,
) -> dict[str, Any]:
    """Map a monitor snapshot + picker list into the UI's state shape.

    `projects` is a list of objects with `.name` and `.path` (e.g.
    `recents.PickerItem`). `snapshot` is a `collect()` result (or None/{} before
    the first poll → a 'loading' state).
    """
    proj_list = [
        {"label": getattr(p, "name", str(p)), "value": str(i), "path": str(getattr(p, "path", ""))}
        for i, p in enumerate(projects)
    ]
    idx = selected_index if 0 <= selected_index < len(projects) else 0
    sel_path = proj_list[idx]["path"] if proj_list else ""

    if not snapshot:
        return {
            "loading": True,
            "projects": proj_list,
            "projectIndex": idx,
            "projectPath": sel_path,
            "phase": busy_phase or "idle",
            "server": {"status": "stopped", "pid": None, "uptimeSeconds": None},
            "bridge": {"status": "unresponsive", "detail": "polling…", "board": "none open"},
            "boardOpen": False,
            "backends": {"plugin": "down", "file": "up", "cli": "idle", "subprocess": "idle"},
            "checklist": {k: False for k in CHECK_ORDER},
            "activity": [],
            "logs": [],
        }

    checklist = _checklist(snapshot)
    bridge = _bridge(snapshot, busy_phase)
    board = snapshot.get("board") or {}
    return {
        "loading": False,
        "projects": proj_list,
        "projectIndex": idx,
        "projectPath": sel_path,
        "phase": _phase(snapshot, checklist, bridge, busy_phase),
        "server": _server(snapshot),
        "bridge": bridge,
        "boardOpen": bool(board.get("open_board")),
        "backends": _backends(snapshot),
        "checklist": checklist,
        "activity": _activity(snapshot),
        "logs": _logs(snapshot),
    }
