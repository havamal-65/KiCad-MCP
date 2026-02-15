"""KiCad library and project path resolution."""

from __future__ import annotations

import os
from pathlib import Path

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.platform_helper import get_platform

logger = get_logger("paths")


def get_kicad_user_dir() -> Path | None:
    """Get the KiCad user configuration/data directory.

    Returns:
        Path to KiCad user directory, or None if not found.
    """
    platform = get_platform()

    if platform == "windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            user_dir = Path(appdata) / "kicad"
            # KiCad 7+ uses versioned dirs
            for version in ["9.0", "8.0", "7.0"]:
                versioned = user_dir / version
                if versioned.exists():
                    return versioned
            if user_dir.exists():
                return user_dir

    elif platform == "macos":
        user_dir = Path.home() / "Library" / "Preferences" / "kicad"
        for version in ["9.0", "8.0", "7.0"]:
            versioned = user_dir / version
            if versioned.exists():
                return versioned
        if user_dir.exists():
            return user_dir

    else:  # linux
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        user_dir = config_home / "kicad"
        for version in ["9.0", "8.0", "7.0"]:
            versioned = user_dir / version
            if versioned.exists():
                return versioned
        if user_dir.exists():
            return user_dir

    return None


def get_system_library_paths() -> list[Path]:
    """Get paths to KiCad's system-installed symbol and footprint libraries.

    Returns:
        List of existing library base directories.
    """
    paths: list[Path] = []
    platform = get_platform()

    # Check KICAD_SYMBOL_DIR and KICAD7_SYMBOL_DIR env vars
    for env_var in ["KICAD_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR",
                    "KICAD7_SYMBOL_DIR"]:
        env_path = os.environ.get(env_var)
        if env_path:
            p = Path(env_path)
            if p.exists():
                paths.append(p)

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            share = Path(program_files) / "KiCad" / version / "share" / "kicad"
            if share.exists():
                for subdir in ["symbols", "footprints", "3dmodels"]:
                    lib_dir = share / subdir
                    if lib_dir.exists():
                        paths.append(lib_dir)

    elif platform == "macos":
        share = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")
        if share.exists():
            for subdir in ["symbols", "footprints", "3dmodels"]:
                lib_dir = share / subdir
                if lib_dir.exists():
                    paths.append(lib_dir)

    else:  # linux
        for share_base in ["/usr/share/kicad", "/usr/local/share/kicad"]:
            share = Path(share_base)
            if share.exists():
                for subdir in ["symbols", "footprints", "3dmodels"]:
                    lib_dir = share / subdir
                    if lib_dir.exists():
                        paths.append(lib_dir)

    return paths


def find_symbol_libraries() -> list[Path]:
    """Find all .kicad_sym symbol library files.

    Returns:
        List of paths to symbol library files.
    """
    libraries: list[Path] = []
    for base_path in get_system_library_paths():
        if base_path.name == "symbols":
            libraries.extend(base_path.glob("*.kicad_sym"))
    return sorted(libraries)


def find_footprint_libraries() -> list[Path]:
    """Find all .pretty footprint library directories.

    Returns:
        List of paths to footprint library directories.
    """
    libraries: list[Path] = []
    for base_path in get_system_library_paths():
        if base_path.name == "footprints":
            libraries.extend(base_path.glob("*.pretty"))
    return sorted(libraries)


def resolve_project_files(project_path: str | Path) -> dict[str, Path | None]:
    """Given a project file or directory, find all related KiCad files.

    Args:
        project_path: Path to .kicad_pro file or project directory.

    Returns:
        Dict with keys: project, board, schematic, and their Paths or None.
    """
    p = Path(project_path).resolve()

    if p.is_file():
        project_dir = p.parent
        project_name = p.stem
    elif p.is_dir():
        project_dir = p
        # Look for .kicad_pro files
        pro_files = list(p.glob("*.kicad_pro"))
        if pro_files:
            project_name = pro_files[0].stem
        else:
            project_name = p.name
    else:
        return {"project": None, "board": None, "schematic": None}

    result: dict[str, Path | None] = {
        "project": None,
        "board": None,
        "schematic": None,
    }

    pro = project_dir / f"{project_name}.kicad_pro"
    if pro.exists():
        result["project"] = pro

    pcb = project_dir / f"{project_name}.kicad_pcb"
    if pcb.exists():
        result["board"] = pcb

    sch = project_dir / f"{project_name}.kicad_sch"
    if sch.exists():
        result["schematic"] = sch

    return result
