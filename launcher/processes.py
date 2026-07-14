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


def _cmdline_port(cmdline: list[str]) -> int | None:
    """The --port value in a server cmdline, or None if not specified."""
    for i, part in enumerate(cmdline):
        s = str(part)
        if s == "--port" and i + 1 < len(cmdline):
            try:
                return int(cmdline[i + 1])
            except (TypeError, ValueError):
                return None
        if s.startswith("--port="):
            try:
                return int(s.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _matches_server(cmdline: list[str], port: int) -> bool:
    """True if a cmdline is a kicad MCP HTTP server bound to `port`.

    Port-aware: a server on a different port is NOT ours for this config
    (an unspecified --port means the 8765 default)."""
    joined = " ".join(str(p) for p in cmdline)
    if _SERVER_MODULE_MARKER not in joined or _SERVER_HTTP_MARKER not in joined:
        return False
    return (_cmdline_port([str(p) for p in cmdline]) or 8765) == port


def _iter_server_pids(cfg: LauncherConfig) -> list[int]:
    try:
        import psutil
    except Exception:
        return []
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if _matches_server(proc.info.get("cmdline") or [], cfg.mcp_port):
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
    if _iter_server_pids(cfg):
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
    pids = _iter_server_pids(cfg)
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


def _pcbnew_pids() -> list[int]:
    try:
        import psutil
    except Exception:
        return []
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == "pcbnew.exe":
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


def stop_pcbnew() -> Result:
    """Close pcbnew (only pcbnew.exe by name — never other KiCad windows)."""
    pids = _pcbnew_pids()
    if not pids:
        return Result("kicad", "already_up", "pcbnew not running")
    try:
        import psutil
    except Exception:
        return Result("kicad", "failed", "psutil unavailable")
    stopped = 0
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
            stopped += 1
        except Exception:
            continue
    return Result("kicad", "stopped", f"closed {stopped} pcbnew process(es)")


def stop_everything(cfg: LauncherConfig) -> list[Result]:
    """Mirror of Start everything: stop the launcher-owned MCP + close pcbnew.
    Never touches processes it can't attribute to the stack."""
    return [stop_mcp_http(cfg), stop_pcbnew()]


def identify_port_owner(cfg: LauncherConfig) -> dict[str, Any] | None:
    """Best-effort: which process is LISTENing on the MCP port? None if unknown
    (psutil absent, access denied, or nothing found)."""
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == cfg.mcp_port
                and conn.pid
            ):
                try:
                    name = psutil.Process(conn.pid).name()
                except Exception:
                    name = "?"
                return {"pid": conn.pid, "name": name}
    except Exception:
        pass
    return None


def stop_foreign_server(cfg: LauncherConfig) -> Result:
    """Stop the identified foreign holder of the MCP port — only that PID."""
    owner = identify_port_owner(cfg)
    if owner is None:
        return Result("mcp", "failed", "could not identify the port owner")
    try:
        import psutil

        psutil.Process(owner["pid"]).terminate()
        return Result("mcp", "stopped", f"terminated {owner['name']} (pid {owner['pid']})")
    except Exception as exc:
        return Result("mcp", "failed", f"{type(exc).__name__}: {exc}")


def reveal_in_explorer(board: Path) -> Result:
    """Open the board's folder in the OS file manager, with the file selected."""
    board = Path(board)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(board)])
        else:
            subprocess.Popen(["xdg-open", str(board.parent)])
        return Result("explorer", "started", str(board.parent))
    except Exception as exc:
        return Result("explorer", "failed", f"{type(exc).__name__}: {exc}")


def reinstall_bridge() -> Result:
    """Run the vetted bridge installer (never reimplemented). Blocking — call
    off the UI thread. User must restart the PCB editor afterwards."""
    script = REPO_ROOT / "kicad_plugin" / "install_bridge.ps1"
    if not script.exists():
        return Result("bridge", "failed", f"{script.name} not found")
    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_detached_flags(),
        )
        if proc.returncode == 0:
            return Result("bridge", "started", "bridge reinstalled — restart the PCB editor")
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return Result("bridge", "failed", tail[-1] if tail else f"exit {proc.returncode}")
    except FileNotFoundError:
        return Result("bridge", "failed", "pwsh not found on PATH")
    except subprocess.TimeoutExpired:
        return Result("bridge", "failed", "installer timed out (120s)")
    except Exception as exc:
        return Result("bridge", "failed", f"{type(exc).__name__}: {exc}")


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
