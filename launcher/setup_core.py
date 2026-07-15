"""Setup wizard core — every check and fix, GUI-free (U3, REQ-CORE-001).

The launcher's Setup view and the future bundled installer (U4) both call this
module; nothing here touches a window. Checks are read-only and never raise;
fixes re-verify by re-running their check and returning the fresh item
(REQ-WIZ-003). The bridge installer is *executed* (`install_bridge.ps1` via
`processes.reinstall_bridge`), never reimplemented (AR1); Claude registration
goes through the `claude mcp` CLI only, never Claude's config files (AR2).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from launcher import processes
from launcher.config import LauncherConfig, connect_info

REPO_ROOT = Path(__file__).resolve().parents[1]

KICAD_DOWNLOAD_URL = "https://www.kicad.org/download/"

Status = Literal["pass", "fail", "stale", "optional_missing", "unknown"]


@dataclass(frozen=True)
class SetupItem:
    key: str  # 'kicad' | 'bridge' | 'claude_cli' | 'claude_mcp' | 'java'
    label: str
    required: bool
    status: Status
    detail: str  # one-line evidence
    fix: str | None  # 'install_bridge' | 'register_claude' | 'open_kicad_download'


@dataclass(frozen=True)
class FixOutcome:
    item: SetupItem  # the re-checked item after the fix ran
    message: str


# --- paths ------------------------------------------------------------------

def _known_folder_documents() -> Path | None:
    """Windows Documents via SHGetKnownFolderPath — OneDrive-aware, matching
    the ps1's ``[Environment]::GetFolderPath("MyDocuments")`` resolution."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        folder_id = _GUID(
            0xFDD39AD0,
            0x238F,
            0x46AF,
            (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
        )
        buf = ctypes.c_wchar_p()
        res = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(buf)
        )
        if res == 0 and buf.value:
            path = Path(buf.value)
            ctypes.windll.ole32.CoTaskMemFree(buf)
            return path
    except Exception:
        return None
    return None


def _documents_dir() -> Path:
    return _known_folder_documents() or Path.home() / "Documents"


def bridge_source_path() -> Path:
    return REPO_ROOT / "kicad_plugin" / "kicad_mcp_bridge.py"


def installed_bridge_path() -> Path:
    return (
        _documents_dir()
        / "KiCad"
        / "9.0"
        / "3rdparty"
        / "plugins"
        / "kicad_mcp_bridge"
        / "__init__.py"
    )


def poison_copies() -> list[Path]:
    """Surviving ``scripting\\plugins`` bridge copies — the sys.modules
    poisoning hazard the installer purges (Step 1a)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    scripting = Path(appdata) / "kicad" / "9.0" / "scripting" / "plugins"
    if not scripting.exists():
        return []
    return sorted(scripting.glob("kicad_mcp_bridge*"))


# --- pure helpers (unit-tested) ----------------------------------------------

_VERSION_RE = re.compile(r'^_BRIDGE_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def _parse_bridge_version(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _classify_bridge(
    source_text: str, installed_text: str | None, poison: bool
) -> tuple[Status, str]:
    if installed_text is None:
        return "fail", "bridge not installed"
    if poison:
        return "stale", "poison copy in scripting\\plugins (sys.modules hazard) — reinstall purges it"
    src_ver = _parse_bridge_version(source_text)
    inst_ver = _parse_bridge_version(installed_text)
    if src_ver != inst_ver:
        return "stale", f"installed {inst_ver or '?'} ≠ source {src_ver or '?'}"
    if installed_text.splitlines() != source_text.splitlines():
        return "stale", f"content drift at same version ({src_ver or '?'})"
    return "pass", f"{inst_ver or 'installed'} — current"


def _registration_payload(cfg: LauncherConfig) -> str:
    """The `claude mcp add-json` argument: the single-server JSON object."""
    servers = connect_info(cfg)["mcpServers"]
    assert isinstance(servers, dict)
    return json.dumps(servers["kicad"])


def _parse_mcp_get(exit_code: int | None, output: str) -> tuple[Status, str]:
    """Classify `claude mcp get kicad`. ``exit_code=None`` means the CLI is
    absent — registration is then impossible but not blocking (the server can
    still be used from other MCP clients)."""
    if exit_code is None:
        return "optional_missing", "claude CLI not installed — register manually from your MCP client"
    if exit_code == 0:
        scope = next(
            (ln.strip() for ln in output.splitlines() if ln.strip().startswith("Scope:")),
            "registered",
        )
        return "pass", scope
    return "fail", "no 'kicad' MCP server registered with Claude Code"


def setup_ok(items: list[SetupItem]) -> bool:
    """True when nothing required is broken. ``optional_missing`` on a required
    item (Claude registration without the CLI) degrades, it doesn't block —
    the server is fully usable from other MCP clients (REQ-WIZ-002/004)."""
    return not any(i.required and i.status in ("fail", "stale") for i in items)


# --- checks (read-only, never raise) ------------------------------------------

def _kicad_version_dir(exe: Path) -> str | None:
    for part in exe.parts:
        if re.fullmatch(r"\d+\.\d+", part):
            return part
    return None


def check_kicad() -> SetupItem:
    try:
        from kicad_mcp.utils import platform_helper

        pcbnew = platform_helper.find_pcbnew_executable()
        cli = platform_helper.find_kicad_cli()
    except Exception:
        pcbnew, cli = None, None
    if pcbnew is None:
        return SetupItem(
            "kicad",
            "KiCad 9",
            True,
            "fail",
            "pcbnew not found — install KiCad 9 from kicad.org",
            "open_kicad_download",
        )
    version = _kicad_version_dir(Path(pcbnew))
    detail = f"pcbnew {version or 'found'} · kicad-cli {'found' if cli else 'missing'}"
    return SetupItem("kicad", "KiCad 9", True, "pass", detail, None)


def check_bridge() -> SetupItem:
    try:
        source = bridge_source_path().read_text(encoding="utf-8")
    except OSError:
        return SetupItem(
            "bridge", "pcbnew bridge", True, "unknown",
            "bridge source missing from this checkout", None,
        )
    installed: str | None = None
    installed_p = installed_bridge_path()
    if installed_p.exists():
        try:
            installed = installed_p.read_text(encoding="utf-8")
        except OSError:
            installed = None
    status, detail = _classify_bridge(source, installed, bool(poison_copies()))
    fix = "install_bridge" if status != "pass" else None
    return SetupItem("bridge", "pcbnew bridge", True, status, detail, fix)


def check_claude_cli() -> SetupItem:
    exe = shutil.which("claude")
    if exe:
        return SetupItem("claude_cli", "Claude Code CLI", False, "pass", exe, None)
    return SetupItem(
        "claude_cli", "Claude Code CLI", False, "optional_missing",
        "claude not on PATH — optional if you only run the server", None,
    )


def _no_window_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def check_claude_mcp(cfg: LauncherConfig) -> SetupItem:
    exe = shutil.which("claude")
    if exe is None:
        status, detail = _parse_mcp_get(None, "")
    else:
        try:
            proc = subprocess.run(
                [exe, "mcp", "get", "kicad"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_no_window_flags(),
            )
            status, detail = _parse_mcp_get(proc.returncode, proc.stdout or "")
        except Exception as exc:
            status, detail = "unknown", f"{type(exc).__name__}: {exc}"
    fix = "register_claude" if status == "fail" else None
    return SetupItem("claude_mcp", "Claude Code registration", True, status, detail, fix)


def check_java() -> SetupItem:
    env_java = os.environ.get("KICAD_MCP_JAVA_PATH")
    java = env_java if env_java and Path(env_java).exists() else shutil.which("java")
    jar = os.environ.get("KICAD_MCP_FREEROUTING_JAR")
    if java:
        detail = f"java: {java}" + (f" · jar: {Path(jar).name}" if jar else "")
        return SetupItem("java", "Java / FreeRouting", False, "pass", detail, None)
    return SetupItem(
        "java", "Java / FreeRouting", False, "optional_missing",
        "no Java found — autorouting unavailable (optional)", None,
    )


def collect_setup(cfg: LauncherConfig) -> list[SetupItem]:
    return [
        check_kicad(),
        check_bridge(),
        check_claude_cli(),
        check_claude_mcp(cfg),
        check_java(),
    ]


# --- fixes (each re-checks; REQ-WIZ-003, REQ-IDEM-001) -------------------------

def fix_install_bridge() -> FixOutcome:
    result = processes.reinstall_bridge()
    item = check_bridge()
    if result.action == "failed":
        return FixOutcome(item, result.reason or "installer failed")
    return FixOutcome(item, result.reason or "bridge installed")


def fix_register_claude(cfg: LauncherConfig) -> FixOutcome:
    current = check_claude_mcp(cfg)
    if current.status == "pass":
        return FixOutcome(current, "already configured — no changes made")
    exe = shutil.which("claude")
    if exe is None:
        return FixOutcome(current, "claude CLI not found — install Claude Code first")
    try:
        proc = subprocess.run(
            [exe, "mcp", "add-json", "kicad", _registration_payload(cfg), "-s", "user"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_no_window_flags(),
        )
    except Exception as exc:
        return FixOutcome(current, f"{type(exc).__name__}: {exc}")
    item = check_claude_mcp(cfg)
    if item.status == "pass":
        return FixOutcome(item, "registered 'kicad' at user scope")
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return FixOutcome(item, tail[-1] if tail else f"exit {proc.returncode}")


def fix_open_kicad_download() -> FixOutcome:
    try:
        webbrowser.open(KICAD_DOWNLOAD_URL)
        message = f"opened {KICAD_DOWNLOAD_URL} — install KiCad 9, then re-check"
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
    return FixOutcome(check_kicad(), message)
