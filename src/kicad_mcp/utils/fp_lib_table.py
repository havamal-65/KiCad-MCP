"""fp-lib-table parsing and footprint-library URI resolution.

KiCad records footprint libraries in two ``fp-lib-table`` files: a global
one in the user config dir and an optional per-project one next to the
``.kicad_pro``. Entry URIs may contain ``${VAR}`` path variables
(``${KIPRJMOD}``, ``${KICAD9_FOOTPRINT_DIR}``, ``${KICAD9_3RD_PARTY}``, or
any environment variable).

The grammar is shared with ``sym-lib-table``; ``parse_lib_table`` handles
both, but symbol-table resolution keeps its existing code path in
``file_backend`` for now.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

from kicad_mcp.logging_config import get_logger

logger = get_logger("fp_lib_table")

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# parse_lib_table results keyed by path string, invalidated by mtime.
_parse_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def get_global_fp_lib_table_path() -> Path | None:
    """Path to the user's global fp-lib-table, or None when absent."""
    from kicad_mcp.utils.kicad_paths import get_kicad_user_dir

    user_dir = get_kicad_user_dir()
    if user_dir is None:
        return None
    table = user_dir / "fp-lib-table"
    return table if table.is_file() else None


def parse_lib_table(path: Path) -> list[dict[str, str]]:
    """Parse a fp-lib-table (or sym-lib-table) into a list of entry dicts.

    Each entry carries the string fields present on its ``(lib ...)`` node
    (``name``, ``type``, ``uri``, ``options``, ``descr``). Entries marked
    ``(disabled)`` are skipped. Returns ``[]`` for unreadable or malformed
    files — a broken table must never take down library search.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _parse_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    from kicad_mcp.utils.sexp_parser import parse_sexp_file

    try:
        tree = parse_sexp_file(path)
    except Exception as exc:
        logger.warning("Cannot parse lib table %s: %s", path, exc)
        return []

    entries: list[dict[str, str]] = []
    for node in tree:
        if not (isinstance(node, list) and node and node[0] == "lib"):
            continue
        entry: dict[str, str] = {}
        disabled = False
        for field in node[1:]:
            if not (isinstance(field, list) and field):
                continue
            if field[0] == "disabled":
                disabled = True
            elif len(field) >= 2 and isinstance(field[0], str):
                entry[field[0]] = str(field[1])
        if not disabled and entry.get("name"):
            entries.append(entry)

    _parse_cache[key] = (mtime, entries)
    return entries


@functools.lru_cache(maxsize=32)
def _default_var_value(var: str) -> str | None:
    """Built-in fallback for KiCad path variables absent from the environment.

    KiCad defines these inside its own process; the MCP server runs outside
    it, so derive the conventional locations. Cached — call
    ``_default_var_value.cache_clear()`` in tests that fake the filesystem.
    """
    if re.fullmatch(r"KICAD\d*_FOOTPRINT_DIR", var):
        from kicad_mcp.utils.kicad_paths import get_system_library_paths

        for base in get_system_library_paths():
            if base.name == "footprints":
                return str(base)
        return None

    # ${KICAD9_3DMODEL_DIR} → the system 3dmodels library dir (§6.6).
    if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", var):
        from kicad_mcp.utils.kicad_paths import get_system_library_paths

        for base in get_system_library_paths():
            if base.name == "3dmodels":
                return str(base)
        return None

    # ${KICAD_USER_3DMODEL_DIR} → user 3rdparty 3dmodels dir, mirroring the
    # KICAD\d+_3RD_PARTY candidate search below but ending in /3dmodels.
    if var == "KICAD_USER_3DMODEL_DIR":
        for version in ("9.0", "8.0", "7.0"):
            candidates: list[Path] = []
            onedrive = os.environ.get("OneDrive")
            if onedrive:
                candidates.append(
                    Path(onedrive) / "Documents" / "KiCad" / version / "3rdparty" / "3dmodels"
                )
            candidates.append(
                Path.home() / "Documents" / "KiCad" / version / "3rdparty" / "3dmodels"
            )
            candidates.append(
                Path.home() / ".local" / "share" / "kicad" / version / "3rdparty" / "3dmodels"
            )
            for c in candidates:
                if c.is_dir():
                    return str(c)
        return None

    m = re.fullmatch(r"KICAD(\d+)_3RD_PARTY", var)
    if m:
        version = f"{m.group(1)}.0"
        candidates: list[Path] = []
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            candidates.append(Path(onedrive) / "Documents" / "KiCad" / version / "3rdparty")
        candidates.append(Path.home() / "Documents" / "KiCad" / version / "3rdparty")
        candidates.append(Path.home() / ".local" / "share" / "kicad" / version / "3rdparty")
        for c in candidates:
            if c.is_dir():
                return str(c)
    return None


def resolve_lib_uri(uri: str, project_dir: Path | None = None) -> Path | None:
    """Expand ``${VAR}`` path variables in a lib-table URI.

    ``${KIPRJMOD}``/``${PROJ_DIR}`` expand to *project_dir*; everything else
    tries the environment first, then KiCad-conventional defaults. Returns
    ``None`` when any variable cannot be resolved (e.g. a project-relative
    URI with no project_dir).
    """

    def _sub(m: re.Match[str]) -> str:
        var = m.group(1)
        if var in ("KIPRJMOD", "PROJ_DIR"):
            if project_dir is None:
                raise KeyError(var)
            return str(project_dir)
        value = os.environ.get(var) or _default_var_value(var)
        if value is None:
            raise KeyError(var)
        return value

    try:
        expanded = _VAR_PATTERN.sub(_sub, uri)
    except KeyError as exc:
        logger.debug("Unresolvable path variable %s in lib URI %r", exc, uri)
        return None
    return Path(expanded)


def get_footprint_library_map(project_dir: str | Path | None = None) -> dict[str, Path]:
    """Map library nickname -> existing ``.pretty`` directory.

    Merge order, later wins: stock install glob (nickname = directory stem),
    global fp-lib-table, project fp-lib-table. Entries whose URI cannot be
    resolved or whose directory does not exist are dropped, so a stale table
    row can never shadow a working stock library.
    """
    from kicad_mcp.utils.kicad_paths import get_system_library_paths

    proj = Path(project_dir) if project_dir is not None else None

    mapping: dict[str, Path] = {}
    for base in get_system_library_paths():
        if base.name == "footprints":
            for lib_dir in base.glob("*.pretty"):
                mapping.setdefault(lib_dir.stem, lib_dir)

    tables: list[Path] = []
    global_table = get_global_fp_lib_table_path()
    if global_table is not None:
        tables.append(global_table)
    if proj is not None:
        project_table = proj / "fp-lib-table"
        if project_table.is_file():
            tables.append(project_table)

    for table_path in tables:
        for entry in parse_lib_table(table_path):
            name = entry.get("name", "")
            uri = entry.get("uri", "")
            if not name or not uri:
                continue
            resolved = resolve_lib_uri(uri, proj)
            if resolved is not None and resolved.is_dir():
                mapping[name] = resolved

    return mapping
