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
# Documents/config/3rdparty resolution lives in the shared resolver
# (kicad_mcp.utils.kicad_version) — one OS-resolved source for the whole repo
# (AR7). The launcher imports those helpers rather than duplicating them.

def bridge_source_path() -> Path:
    """The authoritative bridge source to compare installs against.

    Dev tree: `kicad_plugin/kicad_mcp_bridge.py`. Packaged app (U4): the
    bundled package-data copy (same file the wheel ships).
    """
    from launcher.config import is_frozen

    if is_frozen():
        import kicad_mcp_plugin

        pkg_file: str = kicad_mcp_plugin.__file__ or ""
        return Path(pkg_file).resolve().parent / "data" / "kicad_mcp_bridge.py"
    return REPO_ROOT / "kicad_plugin" / "kicad_mcp_bridge.py"


def installed_bridge_path(version: str) -> Path:
    """PCM-plugins bridge entry point for a KiCad version (config-first path)."""
    from kicad_mcp.utils.kicad_version import plugins_3rdparty_dir

    path: Path = plugins_3rdparty_dir(version) / "kicad_mcp_bridge" / "__init__.py"
    return path


def poison_copies(version: str) -> list[Path]:
    """Surviving ``scripting\\plugins`` bridge copies for a version — the
    sys.modules poisoning hazard the installer purges (Step 1a)."""
    from kicad_mcp.utils.kicad_version import scripting_plugins_dir

    scripting = scripting_plugins_dir(version)
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


def _installed_versions() -> list[str]:
    """Installed KiCad versions newest-first, via the resolver; [] if none."""
    try:
        from kicad_mcp.utils.kicad_version import installed

        return [i.version for i in installed()]
    except Exception:
        return []


def _preferred_version() -> str | None:
    try:
        from kicad_mcp.utils.kicad_version import preferred

        pref = preferred()
        return pref.version if pref is not None else None
    except Exception:
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
            "KiCad",
            True,
            "fail",
            "pcbnew not found — install KiCad from kicad.org",
            "open_kicad_download",
        )
    versions = _installed_versions()
    shown = " + ".join(versions) if versions else (_kicad_version_dir(Path(pcbnew)) or "found")
    detail = f"pcbnew {shown} · kicad-cli {'found' if cli else 'missing'}"
    return SetupItem("kicad", "KiCad", True, "pass", detail, None)


def check_bridge(version: str) -> SetupItem:
    """Bridge status for one installed KiCad version. Required for the preferred
    version (the primary live path); optional for the rest."""
    required = version == _preferred_version()
    key = f"bridge_{version}"
    label = f"pcbnew bridge ({version})"
    try:
        source = bridge_source_path().read_text(encoding="utf-8")
    except OSError:
        return SetupItem(
            key, label, required, "unknown",
            "bridge source missing from this checkout", None,
        )
    installed: str | None = None
    installed_p = installed_bridge_path(version)
    if installed_p.exists():
        try:
            installed = installed_p.read_text(encoding="utf-8")
        except OSError:
            installed = None
    status, detail = _classify_bridge(source, installed, bool(poison_copies(version)))
    # Fix key stays "install_bridge" for every row: the ps1's all-versions
    # default heals all rows in a single run.
    fix = "install_bridge" if status != "pass" else None
    return SetupItem(key, label, required, status, detail, fix)


def _read_api_enable_server(version: str) -> bool | None:
    """``api.enable_server`` from a version's kicad_common.json, or None when
    the file is missing/unreadable or the key is absent."""
    from kicad_mcp.utils.kicad_version import config_dir

    common = config_dir(version) / "kicad_common.json"
    try:
        data = json.loads(common.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    api = data.get("api")
    if isinstance(api, dict) and "enable_server" in api:
        return bool(api["enable_server"])
    return None


def check_api_server(version: str) -> SetupItem:
    """IPC API-server pref for one version. KiCad 10 ships it disabled by
    default; the MCP's primary live path needs it enabled on the preferred
    version. Required for the preferred version, optional for the rest."""
    required = version == _preferred_version()
    key = f"api_server_{version}"
    label = f"IPC API server ({version})"
    pref = _read_api_enable_server(version)
    if pref is True:
        return SetupItem(key, label, required, "pass", "api.enable_server = true", None)
    if pref is False:
        return SetupItem(
            key, label, required, "fail",
            "api.enable_server = false — IPC backend cannot connect",
            f"enable_api_server_{version}",
        )
    return SetupItem(
        key, label, required, "unknown",
        "kicad_common.json missing or no api.enable_server key", None,
    )


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
    items = [check_kicad()]
    versions = _installed_versions()
    if not versions:
        # No install detected — fall back to the preferred/newest string so the
        # checklist still shows an actionable (failing) bridge row.
        fallback = _preferred_version()
        versions = [fallback] if fallback else []
    for version in versions:
        items.append(check_bridge(version))
        items.append(check_api_server(version))
    items.extend([check_claude_cli(), check_claude_mcp(cfg), check_java()])
    return items


# --- fixes (each re-checks; REQ-WIZ-003, REQ-IDEM-001) -------------------------

def fix_install_bridge() -> FixOutcome:
    # The ps1's all-versions default heals every bridge row in one run; we
    # re-check the preferred version's row (the required one) and return it.
    result = processes.reinstall_bridge()
    version = _preferred_version() or (_installed_versions() or ["9.0"])[0]
    item = check_bridge(version)
    if result.action == "failed":
        return FixOutcome(item, result.reason or "installer failed")
    return FixOutcome(item, result.reason or "bridge installed")


def _kicad_version_running(version: str) -> bool | None:
    """Whether a KiCad process for *version* is running.

    True = confirmed running; False = confirmed not running; None = cannot tell
    (psutil absent or the scan failed). The enable_server fix only proceeds on a
    positive False — it must never write config while that version might be open
    (the pcbnew.json/kicad_common.json clobber rule, AR2).
    """
    try:
        import psutil
    except Exception:
        return None
    marker = os.path.join("KiCad", version).lower()  # e.g. "kicad\10.0"
    kicad_images = {"pcbnew.exe", "kicad.exe", "eeschema.exe",
                    "pcbnew", "kicad", "eeschema"}
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            if name not in kicad_images:
                continue
            exe = (proc.info.get("exe") or "").replace("/", "\\").lower()
            if marker in exe:
                return True
        return False
    except Exception:
        return None


def fix_enable_api_server(version: str) -> FixOutcome:
    """Flip ``api.enable_server`` to true for *version*, file-side.

    Refuses unless it can positively confirm that KiCad version is not running
    (AR2 clobber rule): writing kicad_common.json under a live KiCad loses the
    edit on exit. On success, flips the flag and re-checks.
    """
    from kicad_mcp.utils.kicad_version import config_dir

    running = _kicad_version_running(version)
    if running is not False:
        reason = (
            f"close KiCad {version} first — cannot safely edit its config while "
            "it is running"
            if running is True
            else f"cannot confirm KiCad {version} is closed — close it, then retry "
            "(config edits under a live KiCad are lost on exit)"
        )
        return FixOutcome(check_api_server(version), reason)

    common = config_dir(version) / "kicad_common.json"
    try:
        data = json.loads(common.read_text(encoding="utf-8")) if common.exists() else {}
    except (OSError, ValueError) as exc:
        return FixOutcome(check_api_server(version), f"cannot read kicad_common.json: {exc}")

    api = data.get("api")
    if not isinstance(api, dict):
        api = {}
    api["enable_server"] = True
    data["api"] = api
    try:
        common.parent.mkdir(parents=True, exist_ok=True)
        common.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        return FixOutcome(check_api_server(version), f"cannot write kicad_common.json: {exc}")

    item = check_api_server(version)
    msg = (
        f"enabled api.enable_server for KiCad {version} — restart KiCad to apply"
        if item.status == "pass"
        else "wrote the flag but re-check did not confirm it"
    )
    return FixOutcome(item, msg)


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
