#!/usr/bin/env python3
"""
KiCad MCP — Health Monitor (standalone desktop window + JSON mode).

Combines two views of health into one:

  A. The AUTHORITATIVE readiness gate — the exact same checks the MCP tool
     `get_startup_checklist` runs, by importing its underlying
     `run_startup_checklist()` (kicad_running, bridge_reachable, bridge_version_ok,
     pcb_editor_open, kicad_cli_available, project_loaded) -> ready_for_pcb + required_actions.

  B. EXTERNAL signals the gate can't give you — and that keep working even when the
     MCP server itself is down:
       - pcbnew bridge ping details (KiCad ver, pcbnew pid, open board, bridge ver)
       - MCP server process liveness + uptime (`python -m kicad_mcp_plugin`)
       - tool activity      -> tail ~/.kicad-mcp/logs/changes.jsonl
       - warnings / errors  -> tail ~/.kicad-mcp/logs/server.log

Everything shown is read live; nothing is hardcoded. Run under the project venv so the
authoritative checklist is importable (the .ps1 launcher does this).

Usage:
  python scripts/mcp_health_monitor.py           # GUI window
  python scripts/mcp_health_monitor.py --json     # one JSON snapshot, for agents/hooks
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone

import tkinter as tk

try:
    import psutil
except ImportError:
    psutil = None

# The authoritative gate — same function the `get_startup_checklist` MCP tool wraps.
# Imported lazily/gracefully so the monitor still runs (in degraded mode) under a
# python that doesn't have the kicad_mcp package on its path.
try:
    from kicad_mcp.tools.project import run_startup_checklist
except Exception:  # ImportError, or import-time errors in the package
    run_startup_checklist = None

# ---------------------------------------------------------------------------
# Configuration (derived, env-overridable; nothing hardcoded that shouldn't be)
# ---------------------------------------------------------------------------
BRIDGE_HOST = "localhost"
BRIDGE_PORT = int(os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760"))
LOG_DIR = os.path.join(os.path.expanduser("~"), ".kicad-mcp", "logs")
CHANGES_LOG = os.path.join(LOG_DIR, "changes.jsonl")
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
SERVER_PROC_MARKER = "kicad_mcp_plugin"
POLL_SECONDS_DEFAULT = 5.0
BRIDGE_TIMEOUT = 1.5

SERVER_LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(\w+)\] ([\w.]+): (.*)$")

BG = "#0f1420"
CARD = "#1b2436"
FG = "#e6ebf5"
MUTED = "#8a97b1"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"


# ---------------------------------------------------------------------------
# Signal collection (off the UI thread)
# ---------------------------------------------------------------------------
def probe_bridge() -> dict:
    """Ping the pcbnew TCP bridge for rich detail. Never raises."""
    try:
        with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), BRIDGE_TIMEOUT) as s:
            s.settimeout(BRIDGE_TIMEOUT)
            s.sendall(b'{"method":"ping"}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode("utf-8", "replace"))
        result = resp.get("result", resp) if isinstance(resp, dict) else {}
        if not isinstance(result, dict) or not result.get("pong"):
            return {"state": "UNRESPONSIVE", "detail": "connected but no valid pong"}
        app = result.get("app")
        return {
            "state": "HEALTHY" if app == "pcbnew" else "WRONG_OWNER",
            "kicad_version": result.get("kicad_version"),
            "app": app,
            "pid": result.get("pid"),
            "board_path": result.get("board_path"),
            "bridge_version": result.get("bridge_version"),
        }
    except ConnectionRefusedError:
        return {"state": "DOWN", "detail": "port %d refused (pcbnew closed / no bridge)" % BRIDGE_PORT}
    except (socket.timeout, TimeoutError):
        return {"state": "UNRESPONSIVE", "detail": "bridge timed out"}
    except OSError as exc:
        return {"state": "DOWN", "detail": str(exc)}
    except Exception as exc:
        return {"state": "UNRESPONSIVE", "detail": f"{type(exc).__name__}: {exc}"}


def read_checklist() -> dict:
    """Run the authoritative startup checklist (same as the MCP tool). Never raises."""
    if run_startup_checklist is None:
        return {"available": False, "reason": "kicad_mcp not importable (run under project venv)"}
    try:
        r = run_startup_checklist()
        return {
            "available": True,
            "ready_for_pcb": r.get("ready_for_pcb"),
            "checks": r.get("checklist", []),
            "required_actions": r.get("required_actions", []),
        }
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def find_server_process() -> dict:
    if psutil is None:
        return {"running": None, "detail": "psutil unavailable"}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if any(SERVER_PROC_MARKER in str(part) for part in cmd):
                created = proc.info.get("create_time")
                uptime = time.time() - created if created else None
                return {"running": True, "pid": proc.info["pid"], "uptime": uptime}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"running": False}


def _tail_lines(path: str, max_bytes: int = 65536, max_lines: int = 400) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-max_lines:]


def _iso_to_epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def read_activity() -> dict:
    lines = _tail_lines(CHANGES_LOG)
    recent: deque = deque(maxlen=10)
    last = None
    count_hour = 0
    now = time.time()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        epoch = _iso_to_epoch(obj.get("timestamp"))
        if epoch and (now - epoch) <= 3600:
            count_hour += 1
        entry = {"tool": obj.get("tool", "?"), "status": obj.get("status", "success"),
                 "ts": obj.get("timestamp"), "epoch": epoch}
        recent.append(entry)
        last = entry
    return {"last": last, "recent": list(recent), "count_hour": count_hour}


def read_errors() -> dict:
    lines = _tail_lines(SERVER_LOG)
    events: deque = deque(maxlen=14)
    last_start = None
    for line in lines:
        m = SERVER_LOG_RE.match(line)
        if not m:
            continue
        ts, level, name, msg = m.groups()
        if "starting" in msg.lower() and "plugin server" in msg.lower():
            last_start = ts
        if level in ("WARNING", "ERROR", "CRITICAL"):
            events.append({"ts": ts, "level": level, "name": name, "msg": msg})
    return {"events": list(events), "last_start": last_start}


def overall_verdict(bridge_state, running, ready) -> tuple[str, str]:
    """Collapse everything into one verdict + severity (ok | warn | down)."""
    if bridge_state == "DOWN":
        return "DOWN", "down"
    if ready is True:
        return "READY", "ok"
    if ready is False:
        return "NOT READY", "warn"     # bridge up but a gate check failed (e.g. no board)
    # checklist unavailable -> fall back to bridge + server only
    if bridge_state == "HEALTHY" and running is True:
        return "HEALTHY", "ok"
    if bridge_state == "HEALTHY":
        return "BRIDGE ONLY", "warn"
    return "PARTIAL", "warn"


def collect() -> dict:
    snap = {
        "bridge": probe_bridge(),
        "checklist": read_checklist(),
        "server": find_server_process(),
        "activity": read_activity(),
        "errors": read_errors(),
        "polled_at": time.time(),
    }
    label, severity = overall_verdict(
        snap["bridge"].get("state"),
        snap["server"].get("running"),
        snap["checklist"].get("ready_for_pcb"),
    )
    snap["overall"] = label
    snap["severity"] = severity
    return snap


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_uptime(seconds):
    if not seconds or seconds < 0:
        return "—"
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_ago(epoch):
    if not epoch:
        return "—"
    delta = max(0, int(time.time() - epoch))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def short_path(p):
    if not p:
        return "—"
    return os.path.basename(p) or p


def _hint(snap: dict):
    """A short, factual diagnostic derived from live state (no fabrication)."""
    cl = snap["checklist"]
    if cl.get("available") and cl.get("required_actions"):
        return " ".join(cl["required_actions"][:2])
    b = snap["bridge"]
    state = b.get("state")
    if state == "DOWN":
        return ("pcbnew bridge on :%d not reachable. The bridge only binds inside pcbnew.exe "
                "— open KiCad's PCB Editor." % BRIDGE_PORT)
    if state == "WRONG_OWNER":
        return "port %d is held by app=%r, not pcbnew — restart pcbnew." % (BRIDGE_PORT, b.get("app"))
    if state == "UNRESPONSIVE":
        return "bridge connected but did not answer ping — pcbnew may be busy or the bridge stalled."
    if snap["server"].get("running") is False:
        return "no `-m kicad_mcp_plugin` process — the MCP server is not running (Claude Code not connected)."
    return None


def build_report() -> dict:
    """One combined health snapshot shaped for an agent/hook to act on."""
    snap = collect()
    cl = snap["checklist"]
    last = snap["activity"].get("last") or {}
    report = {
        "overall": snap["overall"],
        "severity": snap["severity"],                 # ok | warn | down
        "ready_for_pcb": cl.get("ready_for_pcb"),
        "required_actions": cl.get("required_actions", []),
        "checks": cl.get("checks", []),               # authoritative 6-check gate
        "checklist_available": cl.get("available", False),
        "bridge": snap["bridge"],
        "server_running": snap["server"].get("running"),
        "server_pid": snap["server"].get("pid"),
        "server_uptime_seconds": snap["server"].get("uptime"),
        "last_tool": last.get("tool"),
        "last_tool_at": last.get("ts"),
        "tool_calls_last_hour": snap["activity"].get("count_hour"),
        "recent_errors": snap["errors"].get("events", [])[-6:],
        "server_last_started": snap["errors"].get("last_start"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    hint = _hint(snap)
    if hint:
        report["hint"] = hint
    return report


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------
class Poller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._state: dict = {}
        self._interval = POLL_SECONDS_DEFAULT
        self._stop = threading.Event()
        self._wake = threading.Event()

    @property
    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def set_interval(self, seconds):
        self._interval = max(1.0, seconds)
        self._wake.set()

    def refresh_now(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = collect()
            except Exception as exc:
                snap = {"error": f"{type(exc).__name__}: {exc}", "polled_at": time.time()}
            with self._lock:
                self._state = snap
            self._wake.wait(self._interval)
            self._wake.clear()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class MonitorApp:
    def __init__(self, root: tk.Tk, poller: Poller):
        self.root = root
        self.poller = poller
        root.title("KiCad MCP — Health Monitor")
        root.configure(bg=BG)
        root.geometry("580x780")
        root.minsize(500, 640)

        self._check_rows: dict[str, tuple] = {}
        self._build_header()
        self._build_cards()
        self._build_readiness_panel()
        self._build_tools_panel()
        self._build_log_panel()
        self._build_footer()
        self.root.after(400, self._refresh_ui)

    def _label(self, parent, text, *, fg=FG, font=("Segoe UI", 10), bg=CARD, **kw):
        return tk.Label(parent, text=text, fg=fg, bg=bg, font=font, **kw)

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x", padx=14, pady=(12, 6))
        self._label(hdr, "KiCad MCP — Health", fg=FG, bg=BG,
                    font=("Segoe UI Semibold", 15)).pack(side="left")
        self.overall_pill = self._label(hdr, " CHECKING ", fg="#0b0f18", bg=MUTED,
                                         font=("Segoe UI Semibold", 10))
        self.overall_pill.pack(side="right", ipadx=8, ipady=2)

    def _build_cards(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="x", padx=14, pady=6)
        wrap.columnconfigure(0, weight=1, uniform="c")
        wrap.columnconfigure(1, weight=1, uniform="c")

        self.bridge_card = tk.Frame(wrap, bg=CARD)
        self.bridge_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._label(self.bridge_card, "pcbnew bridge", fg=MUTED, font=("Segoe UI", 9)).pack(
            anchor="w", padx=12, pady=(10, 0))
        self.bridge_state = self._label(self.bridge_card, "…", font=("Segoe UI Semibold", 15))
        self.bridge_state.pack(anchor="w", padx=12)
        self.bridge_detail = self._label(self.bridge_card, "", fg=MUTED,
                                         font=("Consolas", 9), justify="left")
        self.bridge_detail.pack(anchor="w", padx=12, pady=(2, 12))

        self.server_card = tk.Frame(wrap, bg=CARD)
        self.server_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._label(self.server_card, "MCP server process", fg=MUTED, font=("Segoe UI", 9)).pack(
            anchor="w", padx=12, pady=(10, 0))
        self.server_state = self._label(self.server_card, "…", font=("Segoe UI Semibold", 15))
        self.server_state.pack(anchor="w", padx=12)
        self.server_detail = self._label(self.server_card, "", fg=MUTED,
                                         font=("Consolas", 9), justify="left")
        self.server_detail.pack(anchor="w", padx=12, pady=(2, 12))

    def _build_readiness_panel(self):
        frame = tk.Frame(self.root, bg=CARD)
        frame.pack(fill="x", padx=14, pady=6)
        top = tk.Frame(frame, bg=CARD)
        top.pack(fill="x", padx=12, pady=(10, 2))
        self._label(top, "startup checklist  (ready_for_pcb gate)", fg=MUTED,
                    font=("Segoe UI", 9)).pack(side="left")
        self.ready_pill = self._label(top, " … ", fg="#0b0f18", bg=MUTED,
                                      font=("Segoe UI Semibold", 9))
        self.ready_pill.pack(side="right", ipadx=6, ipady=1)

        self.checks_frame = tk.Frame(frame, bg=CARD)
        self.checks_frame.pack(fill="x", padx=12, pady=(2, 4))
        for item in ("kicad_running", "bridge_reachable", "bridge_version_ok",
                     "pcb_editor_open", "kicad_cli_available", "project_loaded"):
            row = tk.Frame(self.checks_frame, bg=CARD)
            row.pack(fill="x")
            dot = self._label(row, "●", fg=MUTED, font=("Segoe UI", 9))
            dot.pack(side="left")
            name = self._label(row, item, fg=FG, font=("Consolas", 9))
            name.pack(side="left", padx=(6, 0))
            self._check_rows[item] = (dot, name)

        self.actions_lbl = self._label(frame, "", fg=AMBER, font=("Segoe UI", 9),
                                       justify="left", wraplength=520)
        self.actions_lbl.pack(anchor="w", padx=12, pady=(2, 10))

    def _build_tools_panel(self):
        frame = tk.Frame(self.root, bg=CARD)
        frame.pack(fill="x", padx=14, pady=6)
        head = tk.Frame(frame, bg=CARD)
        head.pack(fill="x", padx=12, pady=(10, 2))
        self._label(head, "tool activity", fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.activity_line = self._label(head, "—", fg=BLUE, font=("Segoe UI Semibold", 10))
        self.activity_line.pack(side="right")
        self.tools_text = tk.Text(frame, height=6, bg=CARD, fg=FG, bd=0,
                                  font=("Consolas", 9), highlightthickness=0, wrap="none")
        self.tools_text.pack(fill="x", padx=12, pady=(0, 10))
        self.tools_text.configure(state="disabled")

    def _build_log_panel(self):
        frame = tk.Frame(self.root, bg=CARD)
        frame.pack(fill="both", expand=True, padx=14, pady=6)
        self._label(frame, "recent warnings / errors (server.log)", fg=MUTED,
                    font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 2))
        self.log_text = tk.Text(frame, bg="#141b29", fg=FG, bd=0, font=("Consolas", 9),
                                highlightthickness=0, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        for lvl, col in (("WARNING", AMBER), ("ERROR", RED), ("CRITICAL", RED), ("ts", MUTED)):
            self.log_text.tag_configure(lvl, foreground=col)
        self.log_text.configure(state="disabled")

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=14, pady=(4, 12))
        self.status_lbl = self._label(foot, "starting…", fg=MUTED, bg=BG, font=("Segoe UI", 9))
        self.status_lbl.pack(side="left")

        self.topmost = tk.BooleanVar(value=False)
        tk.Checkbutton(foot, text="always on top", variable=self.topmost,
                       command=self._toggle_top, bg=BG, fg=MUTED, selectcolor=CARD,
                       activebackground=BG, activeforeground=FG, bd=0,
                       font=("Segoe UI", 9), highlightthickness=0).pack(side="right")
        tk.Button(foot, text="refresh", command=self.poller.refresh_now, bg=CARD, fg=FG,
                  bd=0, font=("Segoe UI", 9), activebackground="#26324a",
                  activeforeground=FG, padx=10).pack(side="right", padx=(0, 10))
        self._label(foot, "poll:", fg=MUTED, bg=BG, font=("Segoe UI", 9)).pack(
            side="right", padx=(0, 4))
        self.interval_var = tk.StringVar(value=str(int(POLL_SECONDS_DEFAULT)))
        tk.Spinbox(foot, from_=1, to=60, width=3, textvariable=self.interval_var,
                   command=self._change_interval, bg=CARD, fg=FG, bd=0,
                   font=("Segoe UI", 9), justify="center").pack(side="right", padx=(0, 6))

    def _toggle_top(self):
        self.root.attributes("-topmost", self.topmost.get())

    def _change_interval(self):
        try:
            self.poller.set_interval(float(self.interval_var.get()))
        except ValueError:
            pass

    @staticmethod
    def _pill(widget, text, bg):
        widget.configure(text=f" {text} ", bg=bg,
                         fg="#0b0f18" if bg in (GREEN, AMBER, MUTED) else "#ffffff")

    def _refresh_ui(self):
        st = self.poller.state
        if st:
            self._render(st)
        self.root.after(500, self._refresh_ui)

    def _render(self, st: dict):
        if "error" in st and "bridge" not in st:
            self.status_lbl.configure(text=f"poller error: {st['error']}")
            return

        bridge = st.get("bridge", {})
        checklist = st.get("checklist", {})
        server = st.get("server", {})
        activity = st.get("activity", {})
        errors = st.get("errors", {})

        # Bridge card
        bstate = bridge.get("state", "?")
        bcolor = {"HEALTHY": GREEN, "DOWN": RED, "UNRESPONSIVE": AMBER,
                  "WRONG_OWNER": AMBER}.get(bstate, MUTED)
        self.bridge_state.configure(text=bstate, fg=bcolor)
        if bstate in ("HEALTHY", "WRONG_OWNER"):
            detail = (f"KiCad {bridge.get('kicad_version') or '?'}  app={bridge.get('app')}\n"
                      f"pcbnew pid {bridge.get('pid')}\n"
                      f"bridge {bridge.get('bridge_version') or '?'}\n"
                      f"board: {short_path(bridge.get('board_path'))}")
        else:
            detail = bridge.get("detail", "")
        self.bridge_detail.configure(text=detail)

        # Server card
        running = server.get("running")
        if running is True:
            self.server_state.configure(text="RUNNING", fg=GREEN)
            self.server_detail.configure(
                text=f"pid {server.get('pid')}\nuptime {fmt_uptime(server.get('uptime'))}")
        elif running is False:
            self.server_state.configure(text="STOPPED", fg=RED)
            self.server_detail.configure(text="no `-m kicad_mcp_plugin`\nprocess found")
        else:
            self.server_state.configure(text="UNKNOWN", fg=MUTED)
            self.server_detail.configure(text=server.get("detail", ""))

        # Readiness (authoritative checklist)
        self._render_checklist(checklist)

        # Activity
        last = activity.get("last")
        if last:
            self.activity_line.configure(
                text=f"{last['tool']} · {fmt_ago(last.get('epoch'))} · "
                     f"{activity.get('count_hour', 0)}/hr", fg=BLUE)
        else:
            self.activity_line.configure(text="none", fg=MUTED)
        self.tools_text.configure(state="normal")
        self.tools_text.delete("1.0", "end")
        for e in reversed(activity.get("recent", [])):
            ts = (e.get("ts") or "")[11:19]
            mark = "ok" if e.get("status") == "success" else e.get("status", "?")
            self.tools_text.insert("end", f"{ts}  {e['tool']:<30} {mark}\n")
        self.tools_text.configure(state="disabled")

        # Errors
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        evs = errors.get("events", [])
        if not evs:
            self.log_text.insert("end", "no recent warnings or errors ✓\n", "ts")
        for e in evs:
            self.log_text.insert("end", f"{e['ts'][11:]} ", "ts")
            self.log_text.insert("end", f"[{e['level']}] ", e["level"])
            self.log_text.insert("end", f"{e['name'].split('.')[-1]}: {e['msg']}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        # Overall pill
        label, severity = st.get("overall", "?"), st.get("severity", "warn")
        self._pill(self.overall_pill, label, {"ok": GREEN, "warn": AMBER, "down": RED}.get(severity, MUTED))

        polled = st.get("polled_at")
        stamp = datetime.fromtimestamp(polled).strftime("%H:%M:%S") if polled else "—"
        ls = errors.get("last_start")
        self.status_lbl.configure(text=f"updated {stamp}" + (f" · server started {ls}" if ls else ""))

    def _render_checklist(self, checklist: dict):
        if not checklist.get("available"):
            self._pill(self.ready_pill, "N/A", MUTED)
            for dot, name in self._check_rows.values():
                dot.configure(fg=MUTED)
                name.configure(fg=MUTED)
            self.actions_lbl.configure(
                text=checklist.get("reason", "checklist unavailable"), fg=MUTED)
            return
        ready = checklist.get("ready_for_pcb")
        self._pill(self.ready_pill, "READY" if ready else "NOT READY", GREEN if ready else AMBER)
        by_item = {c.get("item"): c for c in checklist.get("checks", [])}
        for item, (dot, name) in self._check_rows.items():
            c = by_item.get(item)
            if not c:
                dot.configure(fg=MUTED)
                name.configure(text=item, fg=MUTED)
                continue
            ok = c.get("status") == "PASS"
            dot.configure(fg=GREEN if ok else RED)
            name.configure(text=f"{item}", fg=FG if ok else RED)
        actions = checklist.get("required_actions") or []
        self.actions_lbl.configure(
            text=("→ " + "  ".join(actions)) if actions else "", fg=AMBER)

    @staticmethod
    def _overall(*a):  # retained for compatibility; verdict now computed in collect()
        return overall_verdict(*a)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="KiCad MCP health monitor (checklist + external signals)")
    ap.add_argument("--json", action="store_true",
                    help="print one combined health snapshot as JSON and exit")
    args = ap.parse_args()

    if args.json:
        print(json.dumps(build_report(), indent=2, default=str))
        return

    poller = Poller()
    poller.start()
    root = tk.Tk()
    MonitorApp(root, poller)
    try:
        root.mainloop()
    finally:
        poller.stop()


if __name__ == "__main__":
    main()
