"""Tkinter launcher window (GUI-only — imports tkinter).

The one window that brings up the stack. Live status comes from a daemon poller
thread that reads `launcher.processes` (KiCad/MCP — what the launcher controls)
and `launcher.status.get_status()` (board, reusing the health monitor). Actions
run off the Tk thread; results and status snapshots are marshalled back to the
UI through a queue and applied on a `root.after` tick.

Nothing this window starts is torn down when the window closes (REQ-WIN-004):
the poller is a daemon thread and launched processes are detached.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from launcher.config import LauncherConfig
from launcher import processes, recents
from launcher import status as status_mod
from launcher.orchestrator import classify_failures, plan_bringup

POLL_INTERVAL_S = 2.5
UI_TICK_MS = 250

_GREEN = "#2ecc71"
_RED = "#e74c3c"
_GREY = "#888888"
_AMBER = "#f39c12"


class LauncherApp:
    def __init__(self, root: tk.Tk, cfg: LauncherConfig) -> None:
        self.root = root
        self.cfg = cfg
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._selected_board: Path | None = None
        self._picker_items: list[recents.PickerItem] = []

        root.title("KiCad-MCP Launcher")
        root.minsize(420, 300)

        self._build_ui()
        self._refresh_picker()
        self._start_poller()
        self.root.after(UI_TICK_MS, self._ui_tick)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        ttk.Label(frm, text="KiCad-MCP Launcher", font=("Segoe UI", 13, "bold")).pack(
            anchor="w"
        )

        rows = ttk.Frame(frm)
        rows.pack(fill="x", padx=10, pady=4)
        self._dots: dict[str, tk.Label] = {}
        self._texts: dict[str, ttk.Label] = {}
        for key, label in (("kicad", "KiCad"), ("mcp", "MCP server"), ("board", "Board")):
            r = ttk.Frame(rows)
            r.pack(fill="x")
            ttk.Label(r, text=f"{label}", width=12).pack(side="left")
            dot = tk.Label(r, text="●", fg=_GREY)
            dot.pack(side="left")
            txt = ttk.Label(r, text="checking…")
            txt.pack(side="left", padx=6)
            self._dots[key] = dot
            self._texts[key] = txt

        picker = ttk.Frame(frm)
        picker.pack(fill="x", padx=10, pady=4)
        ttk.Label(picker, text="Project:").pack(side="left")
        self._picker_var = tk.StringVar()
        self._combo = ttk.Combobox(
            picker, textvariable=self._picker_var, state="readonly"
        )
        self._combo.pack(side="left", fill="x", expand=True, padx=6)
        self._combo.bind("<<ComboboxSelected>>", self._on_pick)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", padx=10, pady=4)
        ttk.Button(btns, text="Start everything", command=self._on_start_everything).pack(
            side="left"
        )
        ttk.Button(btns, text="Restart MCP", command=self._on_restart_mcp).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Stop MCP", command=self._on_stop_mcp).pack(side="left")
        ttk.Button(btns, text="Health monitor", command=self._on_open_monitor).pack(
            side="left", padx=4
        )

        self._ontop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm, text="Always on top", variable=self._ontop_var, command=self._on_ontop
        ).pack(anchor="w", padx=10, pady=4)

        self._log = tk.Text(frm, height=7, width=52, state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True, padx=10, pady=4)

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

    def _on_open_monitor(self) -> None:
        script = self.cfg.venv_pythonw
        monitor = Path(__file__).resolve().parents[1] / "scripts" / "mcp_health_monitor.py"
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        try:
            subprocess.Popen([str(script), str(monitor)], creationflags=flags)
            self._log_line("Opened health monitor.")
        except Exception as exc:
            self._log_line(f"Could not open health monitor: {exc}")

    def _on_ontop(self) -> None:
        self.root.attributes("-topmost", bool(self._ontop_var.get()))

    # --- poller + UI tick --------------------------------------------------

    def _start_poller(self) -> None:
        threading.Thread(target=self._poller, daemon=True).start()

    def _poller(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._collect_snapshot()
                self._events.put(("status", snap))
            except Exception as exc:
                self._events.put(("status_error", str(exc)))
            self._stop.wait(POLL_INTERVAL_S)

    def _collect_snapshot(self) -> dict[str, Any]:
        kicad = processes.pcbnew_running()
        mcp = processes.mcp_http_running(self.cfg)
        board_name: str | None = None
        board_ok = False
        try:
            raw = status_mod.get_status()
            b = raw.get("board") or {}
            board_name = b.get("open_board")
            board_ok = bool(board_name) and b.get("status") != "none"
        except Exception:
            pass
        return {"kicad": kicad, "mcp": mcp, "board_name": board_name, "board_ok": board_ok}

    def _ui_tick(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "status":
                    self._apply_status(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "refresh_picker":
                    self._refresh_picker()
                elif kind == "status_error":
                    self._append_log(f"status error: {payload}")
        except queue.Empty:
            pass
        self.root.after(UI_TICK_MS, self._ui_tick)

    def _apply_status(self, snap: dict[str, Any]) -> None:
        # KiCad
        if snap["kicad"]:
            self._set_row("kicad", _GREEN, "running")
        else:
            self._set_row("kicad", _RED, "not running")
        # MCP
        mcp = snap["mcp"]
        if mcp == "ours":
            self._set_row("mcp", _GREEN, "running")
        elif mcp == "foreign":
            self._set_row("mcp", _AMBER, "port in use (foreign)")
        else:
            self._set_row("mcp", _RED, "stopped")
        # Board
        if snap["board_ok"]:
            self._set_row("board", _GREEN, snap["board_name"] or "loaded")
        elif snap["board_name"]:
            self._set_row("board", _AMBER, f"{snap['board_name']} (impaired)")
        else:
            self._set_row("board", _GREY, "(none)")

    def _set_row(self, key: str, color: str, text: str) -> None:
        self._dots[key].configure(fg=color)
        self._texts[key].configure(text=text)

    def _append_log(self, text: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _on_close(self) -> None:
        self._stop.set()
        self.root.destroy()
