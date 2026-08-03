"""KiCad library and project path resolution."""

from __future__ import annotations

import os
from pathlib import Path

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.kicad_version import config_dir, probe_order, share_dir
from kicad_mcp.utils.platform_helper import get_platform

logger = get_logger("paths")


def get_kicad_user_dir() -> Path | None:
    """Get the KiCad user configuration/data directory.

    Returns:
        Path to KiCad user directory (newest resolvable version), or None.
    """
    for version in probe_order():
        versioned = config_dir(version)
        if versioned.exists():
            return versioned

    # No versioned dir present — fall back to the unversioned base if it exists
    # (config_dir("") drops the empty version part, yielding …/kicad).
    base = config_dir("")
    if base.exists():
        return base
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
        for version in probe_order():
            share = share_dir(version)
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
        candidate_bases = [
            "/usr/share/kicad",
            "/usr/local/share/kicad",
            # Flatpak system-wide install
            "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad",
            # Flatpak per-user install
            str(Path.home() / ".local/share/flatpak/app/org.kicad.KiCad"
                             "/current/active/files/share/kicad"),
        ]
        for share_base in candidate_bases:
            share = Path(share_base)
            if share.exists():
                for subdir in ["symbols", "footprints", "3dmodels"]:
                    lib_dir = share / subdir
                    if lib_dir.exists():
                        paths.append(lib_dir)

    return paths


def find_symbol_libraries() -> list[Path]:
    """Find all .kicad_sym symbol library files.

    On a multi-version install, ``get_system_library_paths`` yields each
    installed version's ``symbols`` directory (newest first, via
    ``probe_order``), plus any ``KICAD*_SYMBOL_DIR`` override ahead of them.
    De-duplicate by file stem so each stock library is returned once — the
    first occurrence (an env override, else the newest version) wins.

    Returns:
        Sorted list of symbol library files, one per library name.
    """
    seen: dict[str, Path] = {}
    for base_path in get_system_library_paths():
        if base_path.name != "symbols":
            continue
        for lib in base_path.glob("*.kicad_sym"):
            seen.setdefault(lib.stem, lib)
    return sorted(seen.values())


def find_footprint_libraries(project_dir: str | Path | None = None) -> list[Path]:
    """Find all .pretty footprint library directories.

    Includes the stock install libraries plus every resolvable entry from
    the global fp-lib-table and, when *project_dir* is given, the project's
    own fp-lib-table.

    Args:
        project_dir: Optional project directory whose fp-lib-table (and
            ${KIPRJMOD}-relative URIs) should be honored.

    Returns:
        Sorted, de-duplicated list of footprint library directories.
    """
    from kicad_mcp.utils.fp_lib_table import get_footprint_library_map
    return sorted(set(get_footprint_library_map(project_dir).values()))


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
