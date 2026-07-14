"""Process wrappers for the launcher — pure of any GUI (no tkinter).

Thin, side-effecting layer over `platform_helper` plus the MCP HTTP server
lifecycle. Everything here is best-effort and returns a `Result`; nothing
raises for an expected condition (missing exe, port in use, etc.). The pure
decision logic lives in `launcher.orchestrator`; this module only *does* things
and *observes* state.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kicad_mcp.utils import platform_helper

from launcher.config import LauncherConfig

REPO_ROOT = Path(__file__).resolve().parents[1]

# cmdline markers that identify a launcher-owned MCP HTTP server.
_SERVER_MODULE_MARKER = "kicad_mcp_plugin"
_SERVER_HTTP_MARKER = "streamable-http"

McpState = Literal["ours", "foreign", "down"]


@dataclass(frozen=True)
class Result:
    piece: str
    action: str  # "started" | "already_up" | "stopped" | "failed"
    reason: str | None = None


def _detached_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    return 0


# --- observation -----------------------------------------------------------

def pcbnew_running() -> bool:
    return bool(platform_helper.is_pcbnew_running())


def claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def _iter_server_pids() -> list[int]:
    try:
        import psutil
    except Exception:
        return []
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(str(p) for p in (proc.info.get("cmdline") or []))
            if _SERVER_MODULE_MARKER in cmd and _SERVER_HTTP_MARKER in cmd:
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return pids


def _port_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mcp_http_running(cfg: LauncherConfig) -> McpState:
    """Classify the MCP HTTP server: our own process, a foreign listener on the
    port, or down."""
    if _iter_server_pids():
        return "ours"
    if _port_reachable(cfg.mcp_host, cfg.mcp_port):
        return "foreign"
    return "down"


def collect_status(cfg: LauncherConfig) -> dict[str, Any]:
    """Snapshot for `orchestrator.plan_bringup`."""
    return {
        "pcbnew_running": pcbnew_running(),
        "mcp_state": mcp_http_running(cfg),
    }


def collect_signals(cfg: LauncherConfig, board: Path | None) -> dict[str, Any]:
    """Snapshot for `orchestrator.classify_failures`."""
    return {
        "pcbnew_exe": platform_helper.find_pcbnew_executable(),
        "claude_available": claude_cli_available(),
        "mcp_state": mcp_http_running(cfg),
        "board": board,
    }


# --- actions ---------------------------------------------------------------

def launch_pcbnew(board: Path) -> Result:
    if pcbnew_running():
        return Result("kicad", "already_up", "pcbnew already running")
    try:
        platform_helper.cleanup_stale_session_files(Path(board).parent)
    except Exception:
        pass  # best-effort; a failed cleanup must not block the launch
    ok = platform_helper.launch_pcbnew(Path(board))
    if ok:
        return Result("kicad", "started", f"opened {Path(board).name}")
    return Result("kicad", "failed", "pcbnew executable not found or launch failed")


def start_mcp_http(cfg: LauncherConfig) -> Result:
    state = mcp_http_running(cfg)
    if state == "ours":
        return Result("mcp", "already_up", "MCP server already running")
    if state == "foreign":
        return Result("mcp", "failed", "port in use by a foreign process")
    try:
        subprocess.Popen(
            [
                str(cfg.venv_python),
                "-m",
                "kicad_mcp_plugin",
                "--transport",
                "streamable-http",
                "--host",
                cfg.mcp_host,
                "--port",
                str(cfg.mcp_port),
            ],
            cwd=str(REPO_ROOT),
            creationflags=_detached_flags(),
        )
        return Result("mcp", "started", f"http://{cfg.mcp_host}:{cfg.mcp_port}/mcp")
    except Exception as exc:
        return Result("mcp", "failed", f"{type(exc).__name__}: {exc}")


def stop_mcp_http(cfg: LauncherConfig) -> Result:
    pids = _iter_server_pids()
    if not pids:
        return Result("mcp", "stopped", "no launcher-owned MCP server running")
    try:
        import psutil
    except Exception:
        return Result("mcp", "failed", "psutil unavailable — cannot stop")
    stopped = 0
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            stopped += 1
        except Exception:
            continue
    psutil.wait_procs([psutil.Process(p) for p in pids if psutil.pid_exists(p)], timeout=5)
    return Result("mcp", "stopped", f"terminated {stopped} process(es)")


def restart_mcp_http(cfg: LauncherConfig) -> Result:
    """Stop then start — a fresh Popen loads current src/ code (REQ-MCP-002)."""
    stop_mcp_http(cfg)
    # Brief settle so the port is released before re-binding.
    import time

    for _ in range(20):
        if not _port_reachable(cfg.mcp_host, cfg.mcp_port, timeout=0.2):
            break
        time.sleep(0.25)
    return start_mcp_http(cfg)


def launch_claude(cfg: LauncherConfig, project_dir: Path) -> Result:
    """Open a Claude Code session wired to the launcher-owned HTTP MCP config.

    Prefers Windows Terminal; falls back to a persistent pwsh window.
    """
    if not claude_cli_available():
        return Result("claude", "failed", "'claude' CLI not found on PATH")
    cfg_path = str(cfg.mcp_config_path)
    claude_args = ["claude", "--strict-mcp-config", "--mcp-config", cfg_path]
    project_dir = Path(project_dir)
    try:
        if shutil.which("wt"):
            subprocess.Popen(
                ["wt", "-d", str(project_dir), *claude_args],
                creationflags=_detached_flags(),
            )
        else:
            joined = " ".join(claude_args)
            subprocess.Popen(
                ["pwsh", "-NoExit", "-Command", joined],
                cwd=str(project_dir),
                creationflags=_detached_flags(),
            )
        return Result("claude", "started", "Claude Code session launched")
    except Exception as exc:
        return Result("claude", "failed", f"{type(exc).__name__}: {exc}")
