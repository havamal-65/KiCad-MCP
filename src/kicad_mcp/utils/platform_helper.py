"""Cross-platform KiCad installation detection."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from kicad_mcp.logging_config import get_logger

logger = get_logger("platform")


def get_platform() -> str:
    """Return the current platform identifier."""
    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "macos"
    return "linux"


def find_kicad_cli() -> Path | None:
    """Find the kicad-cli executable on this system.

    Checks PATH first, then platform-specific default install locations.

    Returns:
        Path to kicad-cli if found, None otherwise.
    """
    # Check PATH first
    cli_in_path = shutil.which("kicad-cli")
    if cli_in_path:
        logger.debug("Found kicad-cli in PATH: %s", cli_in_path)
        return Path(cli_in_path)

    platform = get_platform()
    candidates: list[Path] = []

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in [program_files, program_files_x86]:
            for version in ["9.0", "8.0", "7.0"]:
                candidates.append(Path(base) / "KiCad" / version / "bin" / "kicad-cli.exe")

    elif platform == "macos":
        candidates.extend([
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            Path("/Applications/KiCad/kicad-cli"),
        ])

    else:  # linux
        candidates.extend([
            Path("/usr/bin/kicad-cli"),
            Path("/usr/local/bin/kicad-cli"),
            Path("/snap/kicad/current/usr/bin/kicad-cli"),
        ])

    for candidate in candidates:
        if candidate.exists():
            logger.debug("Found kicad-cli at: %s", candidate)
            return candidate

    logger.debug("kicad-cli not found on this system")
    return None


def find_kicad_python_paths() -> list[Path]:
    """Find KiCad's Python module paths for SWIG bindings (pcbnew).

    Returns:
        List of existing paths that may contain pcbnew module.
    """
    paths: list[Path] = []
    platform = get_platform()

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            p = Path(program_files) / "KiCad" / version / "lib" / "python3" / "dist-packages"
            if p.exists():
                paths.append(p)

    elif platform == "macos":
        base = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                     "Python.framework/Versions")
        if base.exists():
            for pyver in sorted(base.iterdir(), reverse=True):
                sp = pyver / "lib" / f"python{pyver.name}" / "site-packages"
                if sp.exists():
                    paths.append(sp)

    else:  # linux
        linux_paths = [
            Path("/usr/lib/kicad/lib/python3/dist-packages"),
            Path("/usr/lib/python3/dist-packages"),
            Path("/usr/share/kicad/scripting/plugins"),
        ]
        for p in linux_paths:
            if p.exists():
                paths.append(p)

    return paths


def add_kicad_to_sys_path() -> bool:
    """Add KiCad Python paths to sys.path if not already present.

    Returns:
        True if any paths were added.
    """
    added = False
    for p in find_kicad_python_paths():
        p_str = str(p)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)
            logger.debug("Added to sys.path: %s", p_str)
            added = True
    return added


def detect_kicad_version() -> str | None:
    """Try to detect the installed KiCad version.

    Returns:
        Version string like '9.0.1' or None if not detectable.
    """
    import subprocess

    cli = find_kicad_cli()
    if cli:
        try:
            result = subprocess.run(
                [str(cli), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                logger.debug("KiCad version from CLI: %s", version)
                return version
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("Failed to get KiCad version: %s", e)

    return None


def get_platform_info() -> dict:
    """Return comprehensive platform information."""
    return {
        "platform": get_platform(),
        "python_version": sys.version,
        "kicad_cli": str(find_kicad_cli()) if find_kicad_cli() else None,
        "kicad_version": detect_kicad_version(),
        "kicad_python_paths": [str(p) for p in find_kicad_python_paths()],
    }
