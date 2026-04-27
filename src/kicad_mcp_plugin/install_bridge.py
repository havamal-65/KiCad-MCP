"""CLI entry point for `kicad-mcp install-bridge`.

Detects the KiCad plugins directory for the current platform, copies
kicad_mcp_bridge.py into it as a PCM plugin package, patches pcbnew.json
so KiCad auto-loads the bridge on startup, and removes any stale copies
from scripting/plugins/ that would trigger a sys.modules conflict.

Usage:
    kicad-mcp install-bridge [--kicad-version 9.0] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def _find_bridge_source() -> Path | None:
    """Locate kicad_mcp_bridge.py, whether installed via pip or in a dev tree."""
    # pip-installed: file is bundled as package data under kicad_mcp_plugin/data/
    try:
        from importlib.resources import files as _res_files
        candidate = _res_files("kicad_mcp_plugin").joinpath("data/kicad_mcp_bridge.py")
        # In Python 3.9+ files() returns a Traversable; convert to Path if it's on disk
        candidate_path = Path(str(candidate))
        if candidate_path.exists():
            return candidate_path
    except Exception:
        pass

    # Development tree: kicad_plugin/ is a sibling of src/
    here = Path(__file__).resolve().parent           # src/kicad_mcp_plugin/
    dev_path = here.parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"
    if dev_path.exists():
        return dev_path

    return None


def _detect_kicad_dirs(version: str) -> tuple[Path, Path, Path]:
    """Return (plugins_root, scripting_root, pcbnew_json) for the given KiCad version.

    Args:
        version: KiCad version string, e.g. ``"9.0"``.

    Returns:
        Tuple of (3rdparty/plugins root, scripting/plugins root, pcbnew.json path).
    """
    platform = sys.platform

    if platform == "win32":
        docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        plugins_root = docs / "KiCad" / version / "3rdparty" / "plugins"
        scripting_root = appdata / "kicad" / version / "scripting" / "plugins"
        pcbnew_json = appdata / "kicad" / version / "pcbnew.json"
    elif platform == "darwin":
        prefs = Path.home() / "Library" / "Preferences" / "kicad" / version
        plugins_root = prefs / "3rdparty" / "plugins"
        scripting_root = prefs / "scripting" / "plugins"
        pcbnew_json = prefs / "pcbnew.json"
    else:  # linux
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        plugins_root = xdg_data / "kicad" / version / "3rdparty" / "plugins"
        scripting_root = xdg_data / "kicad" / version / "scripting" / "plugins"
        pcbnew_json = xdg_config / "kicad" / version / "pcbnew.json"

    return plugins_root, scripting_root, pcbnew_json


def _remove_stale_scripting_copies(scripting_root: Path, dry_run: bool) -> None:
    """Delete any kicad_mcp_bridge copies under scripting/plugins/.

    KiCad loads scripting/plugins before pcbnew is fully available.  A stale
    bridge there is cached in sys.modules in a broken state, preventing the
    real 3rdparty/plugins copy from starting the TCP server.
    """
    targets = [
        scripting_root / "kicad_mcp_bridge.py",
        scripting_root / "kicad_mcp_bridge",
    ]
    for target in targets:
        if target.exists():
            action = "Would delete" if dry_run else "Deleting"
            print(f"  {action} stale scripting copy: {target}")
            if not dry_run:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()


def _patch_pcbnew_json(pcbnew_json: Path, bridge_dir: Path, dry_run: bool) -> None:
    """Add bridge_dir to action_plugins in pcbnew.json if not already present."""
    plugin_path = bridge_dir.as_posix()

    if pcbnew_json.exists():
        with open(pcbnew_json, encoding="utf-8") as f:
            cfg: dict[str, Any] = json.load(f)
    else:
        print(f"  pcbnew.json not found — will create: {pcbnew_json}")
        cfg = {}

    plugins: list[dict[str, Any]] = cfg.get("action_plugins", [])
    already_present = any(p.get("path") == plugin_path for p in plugins)

    if already_present:
        print("  action_plugins entry already present in pcbnew.json")
        return

    if dry_run:
        print(f"  Would patch pcbnew.json: {pcbnew_json}")
        return

    plugins.append({"path": plugin_path, "show_button": False})
    cfg["action_plugins"] = plugins
    pcbnew_json.parent.mkdir(parents=True, exist_ok=True)
    with open(pcbnew_json, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"  Patched pcbnew.json: {pcbnew_json}")


def install_bridge(version: str = "9.0", dry_run: bool = False) -> int:
    """Install the KiCad MCP bridge plugin.

    Args:
        version: KiCad version string (default ``"9.0"``).
        dry_run: If True, print what would be done without making changes.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    bridge_source = _find_bridge_source()
    if bridge_source is None:
        print(
            "ERROR: kicad_mcp_bridge.py not found.\n"
            "If installed via pip, try reinstalling: pip install --force-reinstall kicad-mcp\n"
            "For a development install, run from the repo root.",
            file=sys.stderr,
        )
        return 1

    plugins_root, scripting_root, pcbnew_json = _detect_kicad_dirs(version)
    bridge_dir = plugins_root / "kicad_mcp_bridge"
    target_file = bridge_dir / "__init__.py"

    print(f"KiCad version  : {version}")
    print(f"Bridge source  : {bridge_source}")
    print(f"Install target : {target_file}")
    print(f"pcbnew.json    : {pcbnew_json}")
    print()

    # Step 1 — remove stale scripting/plugins copies
    _remove_stale_scripting_copies(scripting_root, dry_run)

    # Step 2 — install bridge as PCM plugin package
    if dry_run:
        print(f"  Would install: {target_file}")
    else:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bridge_source, target_file)
        print(f"  Installed: {target_file}")

    # Step 3 — patch pcbnew.json
    _patch_pcbnew_json(pcbnew_json, bridge_dir, dry_run)

    port = os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760")
    print()
    if dry_run:
        print("Dry run complete -- no changes made.")
    else:
        print("Installation complete.")
        print(f"  Plugin directory : {bridge_dir}")
        print(f"  Port             : {port}")
        print()
        print("Next steps:")
        print("  1. Close all KiCad / pcbnew windows.")
        print("  2. Open pcbnew and load any board.")
        print(
            f"  3. Verify bridge: python -c \""
            f"import socket; s=socket.create_connection(('localhost',{port}),2); "
            f"print('bridge OK'); s.close()\""
        )
        print("  4. Restart the MCP server, then call get_backend_info().")

    return 0


def main() -> None:
    """CLI entry point for ``kicad-mcp install-bridge``."""
    parser = argparse.ArgumentParser(
        prog="kicad-mcp install-bridge",
        description=(
            "Install the KiCad MCP bridge plugin into the KiCad PCM plugins directory. "
            "Also patches pcbnew.json so KiCad auto-loads the bridge on startup."
        ),
    )
    parser.add_argument(
        "--kicad-version",
        default="9.0",
        metavar="VERSION",
        help="KiCad version to target (default: 9.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any changes",
    )

    args = parser.parse_args()
    sys.exit(install_bridge(version=args.kicad_version, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
