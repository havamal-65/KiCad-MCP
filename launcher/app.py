"""Unified launcher window (GUI-only — imports tkinter).

ONE window that is both the control surface and the live health panel. It does
not reinvent the health display: it loads the existing
`scripts/mcp_health_monitor.py` and reuses its `Poller` + `MonitorApp` to render
the full rich status (bridge / MCP server / board / checklist / backends /
activity / errors / overall), then injects the launcher controls (project
picker + Start-everything / Restart-MCP / Stop-MCP) above it in the same root.
The monitor file is never edited (AR1); its widgets and poller are reused as-is.

Nothing this window starts is torn down when it closes (REQ-WIN-004): launched
processes are detached; only the poller (which owns no external process) stops.
"""

from __future__ import annotations

import importlib.util
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from launcher.config import LauncherConfig
from launcher import processes, recents
from launcher.orchestrator import classify_failures, plan_bringup

_MONITOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mcp_health_monitor.py"

UI_TICK_MS = 250


def _load_monitor() -> Any:
    """Import the health monitor module by file path (GUI layer only)."""
    spec = importlib.util.spec_from_file_location("kicad_mcp_health_monitor", _MONITOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load health monitor at {_MONITOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LauncherApp:
    def __init__(self, root: tk.Tk, cfg: LauncherConfig) -> None:
        self.root = root
        self.cfg = cfg
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._selected_board: Path | None = None
        self._picker_items: list[recents.PickerItem] = []

        # Reuse the monitor: its theme constants, its poller, and its rich UI.
        self._m = _load_monitor()
        self._poller = self._m.Poller()
        self._poller.start()

        # 1) launcher controls at the top (packed first -> above the health panel)
        self._build_controls()
        self._refresh_picker()

        # 2) the full health panel below, rendered by the monitor's own MonitorApp
        self._monitor_app = self._m.MonitorApp(root, self._poller)

        # MonitorApp set its own title/geometry; make the combined window ours.
        root.title("KiCad-MCP Launcher")
        root.geometry("600x1000")
        root.minsize(520, 820)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(UI_TICK_MS, self._ui_tick)

    # --- controls ----------------------------------------------------------

    def _c(self, name: str, default: str) -> str:
        """Monitor theme color by attribute name, with a fallback."""
        return str(getattr(self._m, name, default))

    def _build_controls(self) -> None:
        bg = self._c("BG", "#0b0f18")
        card = self._c("CARD", "#141b29")
        fg = self._c("FG", "#e6edf3")
        muted = self._c("MUTED", "#8b98a9")

        bar = tk.Frame(self.root, bg=bg)
        bar.pack(fill="x", side="top", padx=14, pady=(12, 0))
        tk.Label(
            bar, text="KiCad-MCP Launcher", bg=bg, fg=fg,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left")

        picker = tk.Frame(self.root, bg=bg)
        picker.pack(fill="x", side="top", padx=14, pady=(8, 4))
        tk.Label(picker, text="Project:", bg=bg, fg=muted, font=("Segoe UI", 9)).pack(
            side="left"
        )
        self._picker_var = tk.StringVar()
        self._combo = ttk.Combobox(
            picker, textvariable=self._picker_var, state="readonly"
        )
        self._combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._combo.bind("<<ComboboxSelected>>", self._on_pick)

        btns = tk.Frame(self.root, bg=bg)
        btns.pack(fill="x", side="top", padx=14, pady=(0, 4))

        def _btn(text: str, cmd: Any, accent: str | None = None) -> tk.Button:
            b = tk.Button(
                btns, text=text, command=cmd, bg=accent or card, fg=fg, bd=0,
                font=("Segoe UI", 9), activebackground="#26324a", activeforeground=fg,
                padx=10, pady=3,
            )
            b.pack(side="left", padx=(0, 8))
            return b

        _btn("Start everything", self._on_start_everything, self._c("GREEN", "#2ecc71"))
        _btn("Restart MCP", self._on_restart_mcp)
        _btn("Stop MCP", self._on_stop_mcp)

        self._action_log = tk.Text(
            self.root, height=4, bg=card, fg=fg, bd=0, font=("Consolas", 9),
            highlightthickness=0, wrap="word",
        )
        self._action_log.pack(fill="x", side="top", padx=14, pady=(2, 6))
        self._action_log.configure(state="disabled")

    # --- picker ------------------------------------------------------------

    def _refresh_picker(self) -> None:
        self._picker_items = recents.list_for_picker(self.cfg)
        labels = [f"{it.name}  ({it.path})" for it in self._picker_items]
        self._combo["values"] = labels
        if self._picker_items and self._selected_board is None:
            self._combo.current(0)
            self._selected_board = self._picker_items[0].path

    def _on_pick(self, _evt: Any = None) -> None:
        idx = self._combo.current()
        if 0 <= idx < len(self._picker_items):
            self._selected_board = self._picker_items[idx].path

    # --- actions (run off the Tk thread) -----------------------------------

    def _run_async(self, target: Any, *args: Any) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()

    def _log_line(self, text: str) -> None:
        self._events.put(("log", text))

    def _on_start_everything(self) -> None:
        self._run_async(self._start_everything_worker, self._selected_board)

    def _start_everything_worker(self, board: Path | None) -> None:
        signals = processes.collect_signals(self.cfg, board)
        diags = classify_failures(signals)
        blocking = [d for d in diags if d.blocking]
        if blocking:
            for d in blocking:
                self._log_line(f"✗ {d.message}")
            return
        for d in diags:
            self._log_line(f"⚠ {d.message}")

        steps = plan_bringup(processes.collect_status(self.cfg), board)
        for step in steps:
            if step.action != "start":
                self._log_line(f"{step.piece}: skip — {step.reason}")
                continue
            if step.piece == "kicad" and board is not None:
                r = processes.launch_pcbnew(board)
            elif step.piece == "mcp":
                r = processes.start_mcp_http(self.cfg)
            elif step.piece == "claude" and board is not None:
                r = processes.launch_claude(self.cfg, board.parent)
            else:
                continue
            reason = f" — {r.reason}" if r.reason else ""
            self._log_line(f"{r.piece}: {r.action}{reason}")

        if board is not None:
            recents.promote(self.cfg, board)
            self._events.put(("refresh_picker", None))
        # Nudge the health panel to re-poll now that things changed.
        try:
            self._poller.refresh_now()
        except Exception:
            pass

    def _on_restart_mcp(self) -> None:
        self._run_async(self._simple_action, "restart")

    def _on_stop_mcp(self) -> None:
        self._run_async(self._simple_action, "stop")

    def _simple_action(self, which: str) -> None:
        if which == "restart":
            self._log_line("Restarting MCP server…")
            r = processes.restart_mcp_http(self.cfg)
        else:
            r = processes.stop_mcp_http(self.cfg)
        reason = f" — {r.reason}" if r.reason else ""
        self._log_line(f"{r.piece}: {r.action}{reason}")
        try:
            self._poller.refresh_now()
        except Exception:
            pass

    # --- UI tick (drains the action-log queue) -----------------------------

    def _ui_tick(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "refresh_picker":
                    self._refresh_picker()
        except queue.Empty:
            pass
        self.root.after(UI_TICK_MS, self._ui_tick)

    def _append_log(self, text: str) -> None:
        self._action_log.configure(state="normal")
        self._action_log.insert("end", text + "\n")
        self._action_log.see("end")
        self._action_log.configure(state="disabled")

    def _on_close(self) -> None:
        try:
            self._poller.stop()
        except Exception:
            pass
        self.root.destroy()
