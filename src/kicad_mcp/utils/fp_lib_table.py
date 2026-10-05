"""fp-lib-table parsing and footprint-library URI resolution.

KiCad records footprint libraries in two ``fp-lib-table`` files: a global
one in the user config dir and an optional per-project one next to the
``.kicad_pro``. Entry URIs may contain ``${VAR}`` path variables
(``${KIPRJMOD}``, ``${KICAD9_FOOTPRINT_DIR}``, ``${KICAD9_3RD_PARTY}``, or
any environment variable).

The grammar is shared with ``sym-lib-table``; ``parse_lib_table`` handles
both, and ``get_symbol_library_map`` resolves symbol libraries the same way
``get_footprint_library_map`` resolves footprints (RW-S4 §3 / F-REC3).
``FileSchematicOps``' own project-symbol resolver keeps its separate
KIR2-S5-verified code path.
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
    return _global_table_path("fp-lib-table")


def _global_table_path(filename: str) -> Path | None:
    """Path to a global lib table in the KiCad settings dir, or None."""
    from kicad_mcp.utils.kicad_paths import get_kicad_user_dir

    user_dir = get_kicad_user_dir()
    if user_dir is None:
        return None
    table = user_dir / filename
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

    # ${KICAD9_SYMBOL_DIR} → the system symbols dir — every stock entry in a
    # global sym-lib-table uses it, so without this default they would all
    # drop to the glob fallback (RW-S4 §3, amendment-logged).
    if re.fullmatch(r"KICAD\d*_SYMBOL_DIR", var):
        from kicad_mcp.utils.kicad_paths import get_system_library_paths

        for base in get_system_library_paths():
            if base.name == "symbols":
                return str(base)
        return None

    # ${KICAD9_3DMODEL_DIR} → the system 3dmodels library dir (§6.6).
    if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", var):
        from kicad_mcp.utils.kicad_paths import get_system_library_paths

        for base in get_system_library_paths():
            if base.name == "3dmodels":
                return str(base)
        return None

    # ${KICAD_USER_3DMODEL_DIR} → user 3rdparty 3dmodels dir. The 3rd-party root
    # comes from the resolver (config-first, then the OS known-folder Documents
    # default) — never an OneDrive literal (AR7).
    if var == "KICAD_USER_3DMODEL_DIR":
        from kicad_mcp.utils.kicad_version import probe_order, third_party_dir

        for version in probe_order():
            candidate = third_party_dir(version) / "3dmodels"
            if candidate.is_dir():
                return str(candidate)
        return None

    # ${KICAD<n>_3RD_PARTY} → that version's 3rd-party root, straight from the
    # resolver (honors a user-relocated KICAD<n>_3RD_PARTY, else the known-folder
    # Documents default).
    m = re.fullmatch(r"KICAD(\d+)_3RD_PARTY", var)
    if m:
        from kicad_mcp.utils.kicad_version import third_party_dir

        candidate = third_party_dir(f"{m.group(1)}.0")
        if candidate.is_dir():
            return str(candidate)
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


def get_symbol_library_map(project_dir: str | Path | None = None) -> dict[str, Path]:
    """Map symbol-library nickname -> existing ``.kicad_sym`` file.

    Symmetric to :func:`get_footprint_library_map` (RW-S4 §3 / F-REC3).
    Merge order, later wins: stock install glob (nickname = file stem),
    global sym-lib-table, project sym-lib-table (``${KIPRJMOD}``/
    ``${PROJ_DIR}`` URIs resolved against *project_dir*). ``(disabled)``
    entries are skipped by ``parse_lib_table``; entries whose URI cannot be
    resolved or whose file does not exist are dropped.
    """
    from kicad_mcp.utils.kicad_paths import find_symbol_libraries

    proj = Path(project_dir) if project_dir is not None else None

    mapping: dict[str, Path] = {}
    for lib_path in find_symbol_libraries():
        mapping.setdefault(lib_path.stem, lib_path)

    tables: list[Path] = []
    global_table = _global_table_path("sym-lib-table")
    if global_table is not None:
        tables.append(global_table)
    if proj is not None:
        project_table = proj / "sym-lib-table"
        if project_table.is_file():
            tables.append(project_table)

    for table_path in tables:
        for entry in parse_lib_table(table_path):
            name = entry.get("name", "")
            uri = entry.get("uri", "")
            if not name or not uri:
                continue
            resolved = resolve_lib_uri(uri, proj)
            if resolved is not None and resolved.is_file():
                mapping[name] = resolved

    return mapping
