"""Single source of truth for KiCad version discovery and per-version paths.

Before K10-S1 the repo carried ``["9.0", "8.0", "7.0"]`` version lists in ~15
places and single-version install targets. This module owns version knowledge
for the whole repo: it enumerates the KiCad installs actually present, picks a
preferred one (newest by default, env- or launcher-overridable), and derives
the per-version directories every discovery site needs.

Design rules that bind callers (KiCad 10 requirements/spec):

* **AR1** — no version list or version-derived path lives outside this module.
  Migrated sites iterate :func:`probe_order` and keep their own exists-checks.
* **AR7** — Windows Documents comes only from the OS known-folder API (the same
  answer KiCad uses), never an ``OneDrive`` env var or a path literal. The
  ``third_party_dir`` is *config-first*: it honors a user-relocated
  ``KICAD<major>_3RD_PARTY`` before falling back to that known-folder default,
  so the resolver always names the directory KiCad itself will scan.

Import-safe, GUI-free, stdlib-only — the launcher imports it at startup.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_mcp.logging_config import get_logger

logger = get_logger("kicad_version")

# Newest-first. The ONLY version list in the repo (AR1). Entries are candidate
# major.minor directory names; an entry resolves to an install only when its
# binaries exist on disk.
KNOWN_VERSIONS = ["10.0", "9.0", "8.0", "7.0"]

# Env override for the preferred version (REQ-VER-002).
ENV_PREFERRED = "KICAD_MCP_KICAD_VERSION"

_VERSION_DIR_RE = re.compile(r"\d+\.\d+")

# Known symbol pin-number renames across KiCad majors (KIR3 FE / REQ-FE-1).
# Outer key: symbol-name prefix, matched against the part of a lib_id after
# the ':' (so "Connector:USB_C_Receptacle_USB2.0_16P" is matched by the
# "USB_C_Receptacle" prefix). Inner mapping: OLD pin number (KiCad 9) ->
# NEW pin number (KiCad 10+). Seed evidence-based renames only — verified
# 2026-08-08 against the installed 9.0/10.0 Connector libraries: every
# USB_C_Receptacle* variant renamed its shield pin S1 -> SH, and no other
# symbol in our builds renamed any pin between 9 and 10.
PIN_RENAMES_9_TO_10: dict[str, dict[str, str]] = {
    "USB_C_Receptacle": {"S1": "SH"},
}


def lookup_pin_rename(lib_id: str, missing_pin: str) -> str | None:
    """The KiCad-10 pin number for *missing_pin* if it is a known 9->10 rename.

    *lib_id* is a full ``Library:Symbol`` id; a bare symbol name is accepted.
    Returns ``None`` when the symbol has no rename table or the pin is not in
    it. Callers must still check the renamed pin actually exists on their
    placed symbol before suggesting it (a wrong symbol may have neither pin).
    """
    sym = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
    for prefix, table in PIN_RENAMES_9_TO_10.items():
        if sym.startswith(prefix):
            renamed = table.get(missing_pin)
            if renamed is not None:
                return renamed
    return None


@dataclass(frozen=True)
class KicadInstall:
    """A KiCad install found on disk, identified by its ``major.minor`` string."""

    version: str
    bin_dir: Path
    pcbnew: Path | None
    kicad_cli: Path | None


# --- module state ------------------------------------------------------------

_installed_cache: list[KicadInstall] | None = None
_preferred_override: str | None = None


def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _program_files_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        val = os.environ.get(env)
        if val:
            roots.append(Path(val))
    if not roots:  # env missing (non-Windows test harness) — conventional default
        roots.append(Path(r"C:\Program Files"))
    return roots


def _exe(bin_dir: Path, stem: str) -> Path | None:
    """Return ``bin_dir/stem(.exe)`` if it exists, honoring the platform suffix."""
    suffix = ".exe" if _platform() == "windows" else ""
    candidate = bin_dir / f"{stem}{suffix}"
    return candidate if candidate.exists() else None


# --- enumeration (REQ-VER-001) ----------------------------------------------

def _enumerate_windows() -> list[KicadInstall]:
    found: dict[str, KicadInstall] = {}
    for root in _program_files_roots():
        kicad_root = root / "KiCad"
        if not kicad_root.is_dir():
            continue
        for child in kicad_root.iterdir():
            if not (child.is_dir() and _VERSION_DIR_RE.fullmatch(child.name)):
                continue
            bin_dir = child / "bin"
            pcbnew = _exe(bin_dir, "pcbnew")
            if pcbnew is None:  # a version dir without pcbnew is not a usable install
                continue
            found.setdefault(
                child.name,
                KicadInstall(child.name, bin_dir, pcbnew, _exe(bin_dir, "kicad-cli")),
            )
    return list(found.values())


def _enumerate_macos() -> list[KicadInstall]:
    # macOS ships a single versionless bundle; synthesize one install from it.
    bundle = Path("/Applications/KiCad/KiCad.app/Contents/MacOS")
    pcbnew = bundle / "pcbnew"
    if not pcbnew.exists():
        return []
    version = _mac_bundle_version() or KNOWN_VERSIONS[0]
    cli = bundle / "kicad-cli"
    return [KicadInstall(version, bundle, pcbnew, cli if cli.exists() else None)]


def _mac_bundle_version() -> str | None:
    plist = Path("/Applications/KiCad/KiCad.app/Contents/Info.plist")
    try:
        text = plist.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(
        r"<key>CFBundleShortVersionString</key>\s*<string>(\d+\.\d+)", text
    )
    return m.group(1) if m else None


def _enumerate_linux() -> list[KicadInstall]:
    # Linux packages install unversioned binaries on PATH; synthesize one.
    pcbnew = shutil.which("pcbnew")
    if pcbnew is None:
        return []
    bin_dir = Path(pcbnew).resolve().parent
    cli = shutil.which("kicad-cli")
    # Linux binaries are unversioned; we don't run them at import time to sniff
    # a version, so label the synthesized install with the newest known string.
    version = KNOWN_VERSIONS[0]
    return [KicadInstall(version, bin_dir, Path(pcbnew), Path(cli) if cli else None)]


def installed(refresh: bool = False) -> list[KicadInstall]:
    """KiCad installs present on disk, newest-first.

    Cached after the first probe; pass ``refresh=True`` to re-scan (the launcher
    rescan path). On this machine returns 10.0 then 9.0 (no 8.0 binaries).
    """
    global _installed_cache
    if _installed_cache is not None and not refresh:
        return _installed_cache
    plat = _platform()
    if plat == "windows":
        found = _enumerate_windows()
    elif plat == "macos":
        found = _enumerate_macos()
    else:
        found = _enumerate_linux()
    found.sort(key=lambda i: _version_key(i.version), reverse=True)
    _installed_cache = found
    return found


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


# --- selection (REQ-VER-002) -------------------------------------------------

def set_preferred(version: str | None) -> None:
    """Pin the preferred version (the launcher version-switcher's backend hook).

    ``None`` clears the pin. A value not currently installed is rejected with a
    warning and leaves the previous pin intact — the switcher never silently
    selects a version KiCad can't run.
    """
    global _preferred_override
    if version is None:
        _preferred_override = None
        return
    if version not in {i.version for i in installed()}:
        logger.warning(
            "set_preferred(%r): not an installed version; keeping %r",
            version, _preferred_override,
        )
        return
    _preferred_override = version


def preferred() -> KicadInstall | None:
    """The selected install: env override → ``set_preferred`` pin → newest.

    An invalid/uninstalled env value warns and falls through (never raises), so
    a stale ``KICAD_MCP_KICAD_VERSION`` degrades to the default instead of
    breaking discovery.
    """
    installs = installed()
    if not installs:
        return None
    by_version = {i.version: i for i in installs}

    env = os.environ.get(ENV_PREFERRED)
    if env:
        if env in by_version:
            return by_version[env]
        logger.warning(
            "%s=%r is not installed (have %s); using newest",
            ENV_PREFERRED, env, ", ".join(by_version),
        )

    if _preferred_override and _preferred_override in by_version:
        return by_version[_preferred_override]

    return installs[0]


def probe_order() -> list[str]:
    """Version strings to try, in order: preferred, other installed, then the
    remaining ``KNOWN_VERSIONS`` tail — deduped.

    This is what migrated discovery sites iterate in place of a literal list.
    The known tail keeps config-only versions resolvable (this machine has an
    8.0 config dir but no 8.0 binaries) exactly as the old hardcoded lists did.
    """
    order: list[str] = []
    pref = preferred()
    if pref is not None:
        order.append(pref.version)
    for inst in installed():
        if inst.version not in order:
            order.append(inst.version)
    for version in KNOWN_VERSIONS:
        if version not in order:
            order.append(version)
    return order


# --- derived paths (REQ-VER-003) ---------------------------------------------

def bin_dir(version: str) -> Path:
    """Binaries dir for a version (no exists-check)."""
    if _platform() == "windows":
        return _program_files_roots()[0] / "KiCad" / version / "bin"
    if _platform() == "macos":
        return Path("/Applications/KiCad/KiCad.app/Contents/MacOS")
    return Path("/usr/bin")


def config_dir(version: str) -> Path:
    """Per-version KiCad config dir (``%APPDATA%\\kicad\\<v>`` / XDG / macOS prefs)."""
    if _platform() == "windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "kicad" / version
    if _platform() == "macos":
        return Path.home() / "Library" / "Preferences" / "kicad" / version
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "kicad" / version


def share_dir(version: str) -> Path:
    """System share/kicad dir holding stock symbols/footprints/templates."""
    if _platform() == "windows":
        return _program_files_roots()[0] / "KiCad" / version / "share" / "kicad"
    if _platform() == "macos":
        return Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")
    return Path("/usr/share/kicad")


def documents_dir() -> Path:
    """The user's Documents dir — Windows via the OS known-folder API (AR7).

    Never reads an ``OneDrive`` env var or a path literal: this returns the same
    location KiCad resolves, wherever Windows redirects Documents.
    """
    if _platform() == "windows":
        known = _known_folder_documents()
        if known is not None:
            return known
    return Path.home() / "Documents"


def _known_folder_documents() -> Path | None:
    """Windows Documents via ``SHGetKnownFolderPath`` (FOLDERID_Documents).

    Moved here from ``launcher/setup_core.py`` so the whole repo shares one
    OS-resolved location. Returns None off-Windows or on any API failure.
    """
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


def _config_3rd_party_var(version: str) -> str | None:
    """That version's ``KICAD<major>_3RD_PARTY`` from ``kicad_common.json``.

    Config-first resolution (REQ-VER-003/INST-005): honor a user who relocated
    their 3rd-party path in KiCad Preferences. Tolerant — a missing file, bad
    JSON, or absent key returns None so the caller uses the default.
    """
    major = version.split(".")[0]
    common = config_dir(version) / "kicad_common.json"
    try:
        data = json.loads(common.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    env_vars = (data.get("environment") or {}).get("vars") or {}
    value = env_vars.get(f"KICAD{major}_3RD_PARTY")
    return value if isinstance(value, str) and value.strip() else None


def third_party_dir(version: str) -> Path:
    """The 3rd-party dir KiCad scans for that version — config-first.

    Reads ``KICAD<major>_3RD_PARTY`` from the version's ``kicad_common.json``
    when set, else the known-folder ``Documents\\KiCad\\<v>\\3rdparty`` default.
    """
    configured = _config_3rd_party_var(version)
    if configured:
        return Path(configured)
    return documents_dir() / "KiCad" / version / "3rdparty"


def plugins_3rdparty_dir(version: str) -> Path:
    """PCM plugins dir (``<3rdparty>/plugins``) — where the bridge installs."""
    return third_party_dir(version) / "plugins"


def scripting_plugins_dir(version: str) -> Path:
    """Legacy ``scripting/plugins`` dir (the sys.modules poison hazard site)."""
    return config_dir(version) / "scripting" / "plugins"
