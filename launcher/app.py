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
import os
import subprocess
import sys
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

# U5 AS-3: the singleton mutex handle — module-level so it lives as long as
# the process (the OS releases it on exit).
_MUTEX_HANDLE: Any = None


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
        # U5 VS-2: apply the persisted KiCad version pin BEFORE anything
        # resolves pcbnew/kicad-cli paths. A persisted version that is no
        # longer installed is cleared (the resolver refuses silent pins).
        self._apply_version_pin(self.settings.get("kicad_version"))
        self._m = _load_monitor()
        self._poller = self._m.Poller()
        self._poller.start()
        # Cached environment facts (refreshed on a version switch).
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
        self._chrome_w: int | None = None
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

    def _screen_width(self) -> int:
        try:
            screens = webview.screens
            if screens:
                return int(screens[0].width) - 72
        except Exception:
            pass
        return 2400

    def fit_size(
        self, content_h: Any, viewport_h: Any, content_w: Any = 0, viewport_w: Any = 0
    ) -> dict[str, Any]:
        """Auto-fit the window to the rendered content: height exactly, width
        grown whenever content would otherwise truncate (never shrunk — the
        user's manual width is the floor). Both clamped to the screen."""
        if self._window is None:
            return {"ok": False}
        try:
            content_h_px = int(content_h)
            viewport_h_px = int(viewport_h)
            content_w_px = int(content_w or 0)
            viewport_w_px = int(viewport_w or 0)
        except (TypeError, ValueError):
            return {"ok": False}
        if self._chrome is None and viewport_h_px > 0 and self._outer_h > 0:
            self._chrome = max(0, self._outer_h - viewport_h_px)
        if self._chrome_w is None and viewport_w_px > 0 and self._width > 0:
            self._chrome_w = max(0, self._width - viewport_w_px)
        if self._max_h is None:
            self._max_h = self._screen_height()
        target_h = max(400, min(content_h_px + (self._chrome or 0), self._max_h))
        target_w = self._width
        if content_w_px > viewport_w_px > 0:
            needed = content_w_px + (self._chrome_w or 0)
            target_w = min(max(self._width, needed), self._screen_width())
        if abs(target_h - self._outer_h) > 2 or abs(target_w - self._width) > 2:
            self._outer_h = target_h
            self._width = target_w
            try:
                self._window.resize(target_w, target_h)
            except Exception:
                pass
        return {"ok": True, "height": target_h, "width": target_w}

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
        # U5: version switcher + auto-surface state.
        from kicad_mcp.utils import kicad_version as kv

        pref = kv.preferred()
        st["kicadVersions"] = [i.version for i in kv.installed()]
        st["kicadVersion"] = pref.version if pref else None
        st["versionNotes"] = self._version_notes()
        st["autoSurface"] = bool(self.settings.get("auto_surface", True))
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

    # ------------------------------------------------------------- U5 version
    def _apply_version_pin(self, version: Any) -> None:
        """Pin the resolver + env to *version* ('' / None = newest default).

        The env var makes spawned children (the MCP server) inherit the pin;
        the in-process pin steers this process's own discovery. A persisted
        version that is no longer installed is cleared from settings.
        """
        from kicad_mcp.utils import kicad_version as kv

        if not version:
            kv.set_preferred(None)
            os.environ.pop(kv.ENV_PREFERRED, None)
            return
        if str(version) not in {i.version for i in kv.installed()}:
            self.settings = settings.save_settings(self.cfg, kicad_version=None)
            return
        kv.set_preferred(str(version))
        os.environ[kv.ENV_PREFERRED] = str(version)

    def _version_notes(self) -> list[str]:
        """Non-blocking warnings for the selected version (VS-4 + VS-5)."""
        from kicad_mcp.utils import kicad_version as kv

        pref = kv.preferred()
        if pref is None:
            return []
        notes: list[str] = []
        others = [v for v in processes.running_pcbnew_versions() if v != pref.version]
        if others:
            notes.append(
                f"KiCad {others[0]} is open and holds the bridge port — "
                f"close it before starting KiCad {pref.version}"
            )
        hint = dashboard.board_format_hint(self._current_board(), pref.version)
        if hint:
            notes.append(hint)
        return notes

    def set_kicad_version(self, version: Any) -> dict[str, Any]:
        """U5 VS-3: select the KiCad install everything targets."""
        from kicad_mcp.utils import kicad_version as kv
        from kicad_mcp.utils.platform_helper import find_pcbnew_executable

        v = str(version).strip() if version else ""
        if v and v not in {i.version for i in kv.installed()}:
            return {"ok": False, "messages": [f"KiCad {v} is not installed"]}
        self._apply_version_pin(v or None)
        self.settings = settings.save_settings(self.cfg, kicad_version=v or None)
        self._pcbnew_exe = find_pcbnew_executable()
        self._setup_cache = None  # wizard rows re-target the selected version
        pref = kv.preferred()
        label = pref.version if pref else "none found"
        msgs = [f"KiCad version: {label}" + ("" if v else " (newest)")]
        msgs += self._version_notes()
        return {"ok": True, "version": label, "messages": msgs}

    def set_auto_surface(self, enabled: Any) -> dict[str, Any]:
        """U5 AS-3: toggle server-started auto-surfacing of this window."""
        val = bool(enabled)
        self.settings = settings.save_settings(self.cfg, auto_surface=val)
        return {
            "ok": True,
            "autoSurface": val,
            "messages": [f"auto-surface {'on' if val else 'off'}"],
        }

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
        key_str = str(key)
        fixes = {
            "install_bridge": setup_core.fix_install_bridge,
            "register_claude": lambda: setup_core.fix_register_claude(self.cfg),
            "open_kicad_download": setup_core.fix_open_kicad_download,
        }
        # Per-version dynamic fix: enable_api_server_<version> (e.g. 10.0).
        if key_str.startswith("enable_api_server_"):
            version = key_str.removeprefix("enable_api_server_")
            outcome = setup_core.fix_enable_api_server(version)
        else:
            fn = fixes.get(key_str)
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


# WebView2 evergreen-runtime client GUID (Microsoft-documented detection key).
_WEBVIEW2_CLIENT_KEY = (
    r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
_WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _webview2_present() -> bool:
    """Whether the WebView2 evergreen runtime is installed (Windows only).

    Registry probe per Microsoft's deployment docs; any readable `pv` value
    in the machine (incl. WOW6432Node) or user hive counts. Non-Windows: True
    (pywebview uses a different backend there).
    """
    if sys.platform != "win32":
        return True
    import winreg

    probes = [
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + _WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + _WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_CURRENT_USER, "Software\\" + _WEBVIEW2_CLIENT_KEY),
    ]
    for hive, key in probes:
        try:
            with winreg.OpenKey(hive, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                if str(version).strip("0. "):
                    return True
        except OSError:
            continue
    return False


def _webview2_missing_dialog() -> None:
    """Native message box (no webview available to render anything better)."""
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        "The Microsoft WebView2 runtime is required to show the launcher "
        f"window and was not found.\n\nInstall it from:\n{_WEBVIEW2_DOWNLOAD_URL}\n\n"
        "OK opens that page in your browser.",
        "KiCad-MCP Launcher — WebView2 missing",
        0x40,  # MB_ICONINFORMATION
    )
    import webbrowser

    webbrowser.open(_WEBVIEW2_DOWNLOAD_URL)


def main() -> None:
    # U5 AS-3: hold the singleton mutex for the window's lifetime so a
    # server-side auto-surface never opens a second launcher. If another
    # launcher already holds it, exit quietly — that window is the product.
    from kicad_mcp.utils.launcher_surface import acquire_launcher_mutex

    global _MUTEX_HANDLE
    _MUTEX_HANDLE = acquire_launcher_mutex()
    if _MUTEX_HANDLE is None and sys.platform == "win32":
        raise SystemExit(0)
    if not _webview2_present():
        _webview2_missing_dialog()
        raise SystemExit(1)
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
