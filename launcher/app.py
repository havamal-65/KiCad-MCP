"""Webview launcher window (GUI layer — imports pywebview).

Renders the dashboard (launcher/ui/index.html) in a WebView2 window and exposes
a Python `LauncherApi` to it. Live status comes from the health monitor's own
`Poller` (reused, not reinvented), reshaped by `launcher.dashboard.build_state`;
actions run through `launcher.processes` / `launcher.orchestrator`. No mock data.

U2 additions: layout variant (console/bento) + window persistence via
`launcher.settings`; workflow actions (stop-everything, browse, rescan, reveal,
copy connect info, reinstall bridge); persistent diagnostics with a
stop-foreign-server action; a status-colored tray icon (pystray, optional) that
also carries bridge drop/recover notifications.

Only this module imports pywebview/pystray; the core stays import-safe.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import webview

from launcher import dashboard, processes, recents, settings, setup_core
from launcher.config import LauncherConfig, connect_info, load_config
from launcher.orchestrator import classify_failures, plan_bringup

_UI_HTML = Path(__file__).resolve().parent / "ui" / "index.html"
_MONITOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mcp_health_monitor.py"

_STATUS_COLOR = {"ok": "#42d17f", "warn": "#f2b13c", "down": "#f2647a"}


def _load_monitor() -> Any:
    spec = importlib.util.spec_from_file_location("kicad_mcp_health_monitor", _MONITOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load health monitor at {_MONITOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(result: processes.Result) -> str:
    return f"{result.piece}: {result.action}" + (f" — {result.reason}" if result.reason else "")


class _Tray:
    """Optional status tray icon (pystray). Degrades to absent if unavailable."""

    def __init__(self, on_show: Any, on_quit: Any) -> None:
        self._icon: Any = None
        self._color = ""
        try:
            import pystray
            from PIL import Image, ImageDraw

            self._pystray = pystray
            self._Image = Image
            self._ImageDraw = ImageDraw
        except Exception:
            self._pystray = None
            return
        menu = self._pystray.Menu(
            self._pystray.MenuItem("Show launcher", on_show, default=True),
            self._pystray.MenuItem("Quit", on_quit),
        )
        self._icon = self._pystray.Icon(
            "kicad-mcp-launcher", self._draw("#5f6b7c"), "KiCad-MCP Launcher", menu
        )
        self._icon.run_detached()

    @property
    def available(self) -> bool:
        return self._icon is not None

    def _draw(self, color: str) -> Any:
        img = self._Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = self._ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, 61, 61], radius=14, fill="#0d1117")
        d.ellipse([18, 18, 46, 46], fill=color)
        return img

    def set_status(self, severity: str, tooltip: str) -> None:
        if self._icon is None:
            return
        color = _STATUS_COLOR.get(severity, "#5f6b7c")
        if color != self._color:
            self._color = color
            try:
                self._icon.icon = self._draw(color)
            except Exception:
                pass
        try:
            self._icon.title = tooltip[:120]
        except Exception:
            pass

    def notify(self, title: str, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass


class LauncherApi:
    """Bridged to the webview as `window.pywebview.api`."""

    def __init__(self, cfg: LauncherConfig) -> None:
        self.cfg = cfg
        self.settings = settings.load_settings(cfg)
        self._projects = recents.list_for_picker(cfg)
        self._selected = 0
        self._busy_phase: str | None = None
        self._busy_until = 0.0
        self._m = _load_monitor()
        self._poller = self._m.Poller()
        self._poller.start()
        # Cached environment facts (don't change while running).
        from kicad_mcp.utils.platform_helper import find_pcbnew_executable
        import shutil

        self._pcbnew_exe = find_pcbnew_executable()
        self._claude_ok = shutil.which("claude") is not None
        # Bridge-transition tracking for notifications.
        self._last_bridge: str | None = None
        # Setup checklist cache — recomputed on demand + after fixes, never on
        # the 1.5 s poll (REQ-CHK-005).
        self._setup_cache: list[setup_core.SetupItem] | None = None
        # Window management.
        self._window: Any = None
        self._outer_h = 0
        self._chrome: int | None = None
        self._max_h: int | None = None
        self._tray = _Tray(self._tray_show, self._tray_quit)

    # ------------------------------------------------------------------ window
    def bind_window(self, window: Any, width: int, height: int) -> None:
        self._window = window
        self._width = width
        self._outer_h = height
        window.events.closing += self._on_closing
        try:
            window.events.resized += self._on_resized
        except Exception:
            pass

    def _on_resized(self, width: int, height: int) -> None:
        self._width = int(width)
        self._outer_h = int(height)

    def _on_closing(self) -> None:
        try:
            key = "width_bento" if self.settings["variant"] == "bento" else "width_console"
            settings.save_settings(
                self.cfg,
                window_x=int(self._window.x),
                window_y=int(self._window.y),
                **{key: int(self._window.width)},
            )
        except Exception:
            pass
        self._tray.stop()

    def _tray_show(self) -> None:
        if self._window is not None:
            try:
                self._window.show()
                self._window.restore()
            except Exception:
                pass

    def _tray_quit(self) -> None:
        try:
            self._on_closing()
        finally:
            if self._window is not None:
                try:
                    self._window.destroy()
                except Exception:
                    pass

    def _screen_height(self) -> int:
        try:
            screens = webview.screens
            if screens:
                return int(screens[0].height) - 72
        except Exception:
            pass
        return 1300

    def fit_height(self, content: Any, viewport: Any) -> dict[str, Any]:
        """Resize the window so the content area exactly fits `content` px."""
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
        target = max(400, min(content_px + (self._chrome or 0), self._max_h))
        if abs(target - self._outer_h) > 2:
            self._outer_h = target
            try:
                self._window.resize(self._width, target)
            except Exception:
                pass
        return {"ok": True, "height": target}

    # ------------------------------------------------------------------ state
    def _busy(self) -> str | None:
        if self._busy_phase and time.time() < self._busy_until:
            return self._busy_phase
        self._busy_phase = None
        return None

    def _current_board(self) -> Path | None:
        if 0 <= self._selected < len(self._projects):
            return self._projects[self._selected].path
        return None

    def _diags(self, mcp_state: str) -> list[dict[str, Any]]:
        signals = {
            "pcbnew_exe": self._pcbnew_exe,
            "claude_available": self._claude_ok,
            "mcp_state": mcp_state,
            "board": self._current_board(),
        }
        out = []
        for d in classify_failures(signals):
            entry: dict[str, Any] = {"code": d.code, "message": d.message, "blocking": d.blocking}
            if d.code == "port_in_use_foreign":
                owner = processes.identify_port_owner(self.cfg)
                if owner:
                    entry["owner"] = f"{owner['name']} (pid {owner['pid']})"
                    entry["action"] = "stopForeign"
            out.append(entry)
        return out

    def get_state(self) -> dict[str, Any]:
        snap = self._poller.state  # {} until first poll
        st = dashboard.build_state(snap, self._projects, self._selected, self._busy())
        st["variant"] = self.settings["variant"]
        st["trayAvailable"] = self._tray.available
        st["diags"] = self._diags(processes.mcp_http_running(self.cfg))
        # Tray color + bridge-transition notification.
        severity = (snap or {}).get("severity") or ("ok" if st["phase"] == "running" else "warn")
        self._tray.set_status(severity, f"KiCad-MCP — {(snap or {}).get('overall', 'starting')}")
        bridge_now = st["bridge"]["status"]
        if self._last_bridge is not None and bridge_now != self._last_bridge:
            if bridge_now == "running":
                self._tray.notify("KiCad-MCP", "pcbnew bridge is back — live board edits available")
            elif self._last_bridge == "running":
                self._tray.notify("KiCad-MCP", "pcbnew bridge lost — live board edits unavailable")
        self._last_bridge = bridge_now
        return st

    # ------------------------------------------------------------------ actions
    def select_project(self, index: Any) -> dict[str, Any]:
        try:
            self._selected = int(index)
        except (TypeError, ValueError):
            pass
        self._poller.refresh_now()
        return {"ok": True}

    def set_variant(self, variant: Any) -> dict[str, Any]:
        v = "bento" if variant == "bento" else "console"
        self.settings = settings.save_settings(self.cfg, variant=v)
        self._width = settings.width_for(self.settings, v)
        if self._window is not None:
            try:
                self._window.resize(self._width, self._outer_h)
            except Exception:
                pass
        return {"ok": True, "variant": v}

    def browse_project(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False}
        dialog = getattr(webview, "FileDialog", None)
        mode = dialog.OPEN if dialog else webview.OPEN_DIALOG  # pywebview 6 / 5 compat
        picked = self._window.create_file_dialog(
            mode, file_types=("KiCad projects (*.kicad_pcb;*.kicad_pro)",)
        )
        if not picked:
            return {"ok": False, "messages": []}
        board = recents.resolve_board_path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if board is None:
            return {"ok": False, "messages": ["no .kicad_pcb found for that selection"]}
        recents.promote(self.cfg, board)
        self._projects = recents.list_for_picker(self.cfg)
        self._selected = 0
        self._poller.refresh_now()
        return {"ok": True, "messages": [f"added {board.name}"]}

    def rescan_projects(self) -> dict[str, Any]:
        self._projects = recents.list_for_picker(self.cfg)
        self._selected = min(self._selected, max(0, len(self._projects) - 1))
        return {"ok": True, "messages": [f"{len(self._projects)} projects"]}

    def reveal_project(self) -> dict[str, Any]:
        board = self._current_board()
        if board is None:
            return {"ok": False, "messages": ["no project selected"]}
        r = processes.reveal_in_explorer(board)
        return {"ok": r.action != "failed", "messages": [_fmt(r)] if r.action == "failed" else []}

    def copy_connect_info(self) -> dict[str, Any]:
        payload = json.dumps(connect_info(self.cfg), indent=2)
        try:
            subprocess.run(
                ["clip"],
                input=payload,
                text=True,
                timeout=10,
                creationflags=processes._detached_flags(),
            )
            return {"ok": True, "messages": ["MCP config copied to clipboard"]}
        except Exception as exc:
            return {"ok": False, "messages": [f"clipboard failed: {exc}"]}

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

    def stop_everything(self) -> dict[str, Any]:
        self._busy_phase = None
        results = processes.stop_everything(self.cfg)
        self._poller.refresh_now()
        return {"ok": True, "messages": [_fmt(r) for r in results]}

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

    def stop_foreign_server(self) -> dict[str, Any]:
        r = processes.stop_foreign_server(self.cfg)
        self._poller.refresh_now()
        return {"ok": r.action != "failed", "messages": [_fmt(r)]}

    def reinstall_bridge(self) -> dict[str, Any]:
        r = processes.reinstall_bridge()
        return {"ok": r.action != "failed", "messages": [_fmt(r)]}

    def open_pcb_editor(self) -> dict[str, Any]:
        board = self._current_board()
        if board is None:
            return {"ok": False, "messages": ["no project selected"]}
        self._busy_phase = "starting"
        self._busy_until = time.time() + 12
        r = processes.launch_pcbnew(board)
        self._poller.refresh_now()
        return {"ok": r.action != "failed", "messages": [_fmt(r)]}

    # ------------------------------------------------------------------ setup
    def get_setup_state(self, refresh: Any = False) -> dict[str, Any]:
        """Setup checklist (U3). pywebview dispatches api calls on worker
        threads, so the (slow) CLI/FS checks never block the UI or the poll."""
        if self._setup_cache is None or refresh:
            self._setup_cache = setup_core.collect_setup(self.cfg)
        items = self._setup_cache
        return {
            "items": [dataclasses.asdict(i) for i in items],
            "ok": setup_core.setup_ok(items),
        }

    def run_fix(self, key: Any) -> dict[str, Any]:
        fixes = {
            "install_bridge": setup_core.fix_install_bridge,
            "register_claude": lambda: setup_core.fix_register_claude(self.cfg),
            "open_kicad_download": setup_core.fix_open_kicad_download,
        }
        fn = fixes.get(str(key))
        if fn is None:
            return {"ok": False, "messages": [f"unknown fix: {key}"]}
        outcome = fn()
        if self._setup_cache is not None:
            self._setup_cache = [
                outcome.item if i.key == outcome.item.key else i for i in self._setup_cache
            ]
        return {
            "ok": outcome.item.status == "pass",
            "item": dataclasses.asdict(outcome.item),
            "messages": [outcome.message],
        }

    def hide_to_tray(self) -> dict[str, Any]:
        if not self._tray.available:
            return {"ok": False, "messages": ["tray unavailable (pystray not installed)"]}
        if self._window is not None:
            try:
                self._window.hide()
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "messages": [str(exc)]}
        return {"ok": False}


def main() -> None:
    cfg = load_config()
    api = LauncherApi(cfg)
    st = api.settings
    width = settings.width_for(st, st["variant"])
    height = 900
    kwargs: dict[str, Any] = {}
    if st["window_x"] is not None and st["window_y"] is not None:
        kwargs["x"] = int(st["window_x"])
        kwargs["y"] = int(st["window_y"])
    window = webview.create_window(
        "KiCad-MCP Launcher",
        str(_UI_HTML),
        js_api=api,
        width=width,
        height=height,
        min_size=(460, 420),
        background_color="#0d1117",
        **kwargs,
    )
    api.bind_window(window, width, height)
    webview.start()


if __name__ == "__main__":
    main()
