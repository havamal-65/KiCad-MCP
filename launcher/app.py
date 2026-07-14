"""Webview launcher window (GUI layer — imports pywebview).

Renders the design's real HTML/CSS (launcher/ui/index.html) in a WebView2
window and exposes a Python `LauncherApi` to it. Live status comes from the
health monitor's own `Poller` (reused, not reinvented), reshaped by
`launcher.dashboard.build_state` into the UI's state; actions run through
`launcher.processes` / `launcher.orchestrator`. No mock data — the JS renders
whatever real state Python hands it.

Only this module imports pywebview; the core (`config`/`recents`/`orchestrator`/
`processes`/`dashboard`) stays import-safe and GUI-free.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from typing import Any

import webview

from launcher import dashboard, processes, recents
from launcher.config import LauncherConfig, load_config
from launcher.orchestrator import classify_failures, plan_bringup

_UI_HTML = Path(__file__).resolve().parent / "ui" / "index.html"
_MONITOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mcp_health_monitor.py"


def _load_monitor() -> Any:
    spec = importlib.util.spec_from_file_location("kicad_mcp_health_monitor", _MONITOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load health monitor at {_MONITOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(result: processes.Result) -> str:
    return f"{result.piece}: {result.action}" + (f" — {result.reason}" if result.reason else "")


class LauncherApi:
    """Bridged to the webview as `window.pywebview.api`."""

    def __init__(self, cfg: LauncherConfig) -> None:
        self.cfg = cfg
        self._projects = recents.list_for_picker(cfg)
        self._selected = 0
        self._busy_phase: str | None = None
        self._busy_until = 0.0
        self._m = _load_monitor()
        self._poller = self._m.Poller()
        self._poller.start()
        # Window auto-fit state (set by bind_window; used by fit_height).
        self._window: Any = None
        self._width = 0
        self._outer_h = 0
        self._chrome: int | None = None  # native title-bar/border height, computed once
        self._max_h: int | None = None

    def bind_window(self, window: Any, width: int, height: int) -> None:
        self._window = window
        self._width = width
        self._outer_h = height

    def _screen_height(self) -> int:
        try:
            screens = webview.screens
            if screens:
                return int(screens[0].height) - 72  # leave room for the taskbar
        except Exception:
            pass
        return 1300

    # --- helpers ---
    def _busy(self) -> str | None:
        if self._busy_phase and time.time() < self._busy_until:
            return self._busy_phase
        self._busy_phase = None
        return None

    def _current_board(self) -> Path | None:
        if 0 <= self._selected < len(self._projects):
            return self._projects[self._selected].path
        return None

    # --- exposed to JS ---
    def get_state(self) -> dict[str, Any]:
        snap = self._poller.state  # {} until first poll
        return dashboard.build_state(snap, self._projects, self._selected, self._busy())

    def fit_height(self, content: Any, viewport: Any) -> dict[str, Any]:
        """Resize the window so its content area exactly fits `content` px.

        `viewport` is the webview's inner height; the native chrome (title bar +
        borders) is the difference between the outer window and the viewport,
        computed once and reused. Clamped to the screen height."""
        if self._window is None:
            return {"ok": False}
        try:
            content_px = int(content)
            viewport_px = int(viewport)
        except (TypeError, ValueError):
            return {"ok": False}
        if self._chrome is None and viewport_px > 0 and self._outer_h > 0:
            self._chrome = max(0, self._outer_h - viewport_px)
        if self._max_h is None:
            self._max_h = self._screen_height()
        target = content_px + (self._chrome or 0)
        target = max(400, min(target, self._max_h))
        if abs(target - self._outer_h) > 2:
            self._outer_h = target
            try:
                self._window.resize(self._width, target)
            except Exception:
                pass
        return {"ok": True, "height": target}

    def select_project(self, index: Any) -> dict[str, Any]:
        try:
            self._selected = int(index)
        except (TypeError, ValueError):
            pass
        self._poller.refresh_now()
        return {"ok": True}

    def start_everything(self) -> dict[str, Any]:
        board = self._current_board()
        diags = classify_failures(processes.collect_signals(self.cfg, board))
        blocking = [d for d in diags if d.blocking]
        if blocking:
            return {"ok": False, "messages": [d.message for d in blocking]}
        self._busy_phase = "starting"
        self._busy_until = time.time() + 14
        warnings = [d.message for d in diags if not d.blocking]
        threading.Thread(target=self._do_start, args=(board,), daemon=True).start()
        return {"ok": True, "messages": warnings + ["starting…"]}

    def _do_start(self, board: Path | None) -> None:
        try:
            for step in plan_bringup(processes.collect_status(self.cfg), board):
                if step.action != "start":
                    continue
                if step.piece == "kicad" and board is not None:
                    processes.launch_pcbnew(board)
                elif step.piece == "mcp":
                    processes.start_mcp_http(self.cfg)
                elif step.piece == "claude" and board is not None:
                    processes.launch_claude(self.cfg, board.parent)
            if board is not None:
                recents.promote(self.cfg, board)
                self._projects = recents.list_for_picker(self.cfg)
        finally:
            self._poller.refresh_now()

    def restart_mcp(self) -> dict[str, Any]:
        self._busy_phase = "restarting"
        self._busy_until = time.time() + 9
        threading.Thread(target=self._do_restart, daemon=True).start()
        return {"ok": True, "messages": ["restarting MCP server…"]}

    def _do_restart(self) -> None:
        try:
            processes.restart_mcp_http(self.cfg)
        finally:
            self._poller.refresh_now()

    def stop_mcp(self) -> dict[str, Any]:
        self._busy_phase = None
        r = processes.stop_mcp_http(self.cfg)
        self._poller.refresh_now()
        return {"ok": True, "messages": [_fmt(r)]}

    def open_pcb_editor(self) -> dict[str, Any]:
        board = self._current_board()
        if board is None:
            return {"ok": False, "messages": ["no project selected"]}
        self._busy_phase = "starting"
        self._busy_until = time.time() + 12
        r = processes.launch_pcbnew(board)
        self._poller.refresh_now()
        return {"ok": r.action != "failed", "messages": [_fmt(r)]}


def main() -> None:
    cfg = load_config()
    api = LauncherApi(cfg)
    width, height = 504, 900
    window = webview.create_window(
        "KiCad-MCP Launcher",
        str(_UI_HTML),
        js_api=api,
        width=width,
        height=height,
        min_size=(460, 420),
        background_color="#0d1117",
    )
    api.bind_window(window, width, height)
    webview.start()


if __name__ == "__main__":
    main()
