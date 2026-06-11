"""Tests for fp-lib-table parsing and URI resolution (known-issues fix #1)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.utils.fp_lib_table import (
    _default_var_value,
    get_footprint_library_map,
    get_global_fp_lib_table_path,
    parse_lib_table,
    resolve_lib_uri,
)

SAMPLE_TABLE = """\
(fp_lib_table
  (version 7)
  (lib (name Audio_Module)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty)(options "")(descr "Audio Module footprints"))
  (lib (name Seeed_XIAO)(type KiCad)(uri "C:/parts/Seeed_Studio_XIAO_Series.pretty")(options "")(descr "Seeed Studio XIAO Series"))
  (lib (name AirQuality_Project)(type KiCad)(uri "${KIPRJMOD}/AirQuality.pretty")(options "")(descr ""))
  (lib (name OldLib)(type KiCad)(uri "C:/old/OldLib.pretty")(options "")(descr "")(disabled))
)
"""


@pytest.fixture(autouse=True)
def _clear_default_var_cache():
    _default_var_value.cache_clear()
    yield
    _default_var_value.cache_clear()


# ---------------------------------------------------------------------------
# parse_lib_table
# ---------------------------------------------------------------------------

def test_parse_lib_table_basic(tmp_path: Path):
    table = tmp_path / "fp-lib-table"
    table.write_text(SAMPLE_TABLE, encoding="utf-8")

    entries = parse_lib_table(table)
    names = [e["name"] for e in entries]

    assert names == ["Audio_Module", "Seeed_XIAO", "AirQuality_Project"]
    assert entries[0]["uri"] == "${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty"
    assert entries[1]["uri"] == "C:/parts/Seeed_Studio_XIAO_Series.pretty"
    assert entries[0]["type"] == "KiCad"
    assert entries[0]["descr"] == "Audio Module footprints"


def test_parse_lib_table_skips_disabled(tmp_path: Path):
    table = tmp_path / "fp-lib-table"
    table.write_text(SAMPLE_TABLE, encoding="utf-8")

    names = [e["name"] for e in parse_lib_table(table)]
    assert "OldLib" not in names


def test_parse_lib_table_missing_file(tmp_path: Path):
    assert parse_lib_table(tmp_path / "fp-lib-table") == []


def test_parse_lib_table_malformed(tmp_path: Path):
    table = tmp_path / "fp-lib-table"
    table.write_text("this is not an s-expression", encoding="utf-8")
    assert parse_lib_table(table) == []


def test_parse_lib_table_cache_invalidates_on_mtime(tmp_path: Path):
    table = tmp_path / "fp-lib-table"
    table.write_text('(fp_lib_table (lib (name A)(type KiCad)(uri "x")))', encoding="utf-8")
    first = parse_lib_table(table)
    assert [e["name"] for e in first] == ["A"]

    table.write_text('(fp_lib_table (lib (name B)(type KiCad)(uri "x")))', encoding="utf-8")
    # Force a distinct mtime — same-second writes can share a timestamp.
    stat = table.stat()
    os.utime(table, (stat.st_atime, stat.st_mtime + 10))

    second = parse_lib_table(table)
    assert [e["name"] for e in second] == ["B"]


def test_parse_lib_table_handles_sym_lib_table_grammar(tmp_path: Path):
    table = tmp_path / "sym-lib-table"
    table.write_text(
        '(sym_lib_table (version 7) '
        '(lib (name Device)(type KiCad)(uri ${KICAD9_SYMBOL_DIR}/Device.kicad_sym)(options "")(descr "")))',
        encoding="utf-8",
    )
    entries = parse_lib_table(table)
    assert [e["name"] for e in entries] == ["Device"]


# ---------------------------------------------------------------------------
# resolve_lib_uri
# ---------------------------------------------------------------------------

def test_resolve_absolute_uri_passthrough():
    assert resolve_lib_uri("C:/parts/X.pretty") == Path("C:/parts/X.pretty")


def test_resolve_kiprjmod(tmp_path: Path):
    resolved = resolve_lib_uri("${KIPRJMOD}/libs/Foo.pretty", project_dir=tmp_path)
    assert resolved == tmp_path / "libs" / "Foo.pretty"


def test_resolve_kiprjmod_without_project_dir_returns_none():
    assert resolve_lib_uri("${KIPRJMOD}/libs/Foo.pretty") is None


def test_resolve_env_var(monkeypatch):
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", "D:/kicad/footprints")
    resolved = resolve_lib_uri("${KICAD9_FOOTPRINT_DIR}/R.pretty")
    assert resolved == Path("D:/kicad/footprints/R.pretty")


def test_resolve_generic_env_var(monkeypatch):
    monkeypatch.setenv("MY_PARTS", "D:/myparts")
    assert resolve_lib_uri("${MY_PARTS}/X.pretty") == Path("D:/myparts/X.pretty")


def test_resolve_unknown_var_returns_none(monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR_12345", raising=False)
    assert resolve_lib_uri("${NO_SUCH_VAR_12345}/X.pretty") is None


def test_resolve_footprint_dir_default_from_stock(monkeypatch, tmp_path: Path):
    """Without the env var, ${KICAD9_FOOTPRINT_DIR} falls back to the stock dir."""
    monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
    stock = tmp_path / "footprints"
    stock.mkdir()
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        return_value=[stock],
    ):
        resolved = resolve_lib_uri("${KICAD9_FOOTPRINT_DIR}/R.pretty")
    assert resolved == stock / "R.pretty"


def test_resolve_3rd_party_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("KICAD9_3RD_PARTY", raising=False)
    monkeypatch.delenv("OneDrive", raising=False)
    third_party = tmp_path / "Documents" / "KiCad" / "9.0" / "3rdparty"
    third_party.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolved = resolve_lib_uri("${KICAD9_3RD_PARTY}/footprints/X.pretty")
    assert resolved == third_party / "footprints" / "X.pretty"


# ---------------------------------------------------------------------------
# get_global_fp_lib_table_path
# ---------------------------------------------------------------------------

def test_global_table_path(tmp_path: Path):
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table)", encoding="utf-8")
    with patch("kicad_mcp.utils.kicad_paths.get_kicad_user_dir", return_value=tmp_path):
        assert get_global_fp_lib_table_path() == tmp_path / "fp-lib-table"


def test_global_table_path_absent(tmp_path: Path):
    with patch("kicad_mcp.utils.kicad_paths.get_kicad_user_dir", return_value=tmp_path):
        assert get_global_fp_lib_table_path() is None


def test_global_table_path_no_user_dir():
    with patch("kicad_mcp.utils.kicad_paths.get_kicad_user_dir", return_value=None):
        assert get_global_fp_lib_table_path() is None


# ---------------------------------------------------------------------------
# get_footprint_library_map — merge order
# ---------------------------------------------------------------------------

def _make_env(tmp_path: Path) -> dict[str, Path]:
    """Fake stock dir + global table + project with its own table.

    The nickname "Shared" exists at all three levels so merge order is
    observable; each level also has a unique nickname.
    """
    stock_base = tmp_path / "footprints"
    (stock_base / "Shared.pretty").mkdir(parents=True)
    (stock_base / "StockOnly.pretty").mkdir()

    global_shared = tmp_path / "global" / "Shared.pretty"
    global_shared.mkdir(parents=True)
    global_custom = tmp_path / "global" / "Seeed_XIAO.pretty"
    global_custom.mkdir()
    global_table = tmp_path / "global" / "fp-lib-table"
    global_table.write_text(
        "(fp_lib_table\n"
        f'  (lib (name Shared)(type KiCad)(uri "{global_shared.as_posix()}")(options "")(descr ""))\n'
        f'  (lib (name Seeed_XIAO)(type KiCad)(uri "{global_custom.as_posix()}")(options "")(descr ""))\n'
        '  (lib (name Ghost)(type KiCad)(uri "C:/does/not/exist.pretty")(options "")(descr ""))\n'
        ")",
        encoding="utf-8",
    )

    project = tmp_path / "project"
    (project / "Shared.pretty").mkdir(parents=True)
    (project / "AirQuality.pretty").mkdir()
    (project / "fp-lib-table").write_text(
        "(fp_lib_table\n"
        '  (lib (name Shared)(type KiCad)(uri "${KIPRJMOD}/Shared.pretty")(options "")(descr ""))\n'
        '  (lib (name AirQuality_Project)(type KiCad)(uri "${KIPRJMOD}/AirQuality.pretty")(options "")(descr ""))\n'
        ")",
        encoding="utf-8",
    )

    return {
        "stock_base": stock_base,
        "global_table": global_table,
        "project": project,
        "global_shared": global_shared,
        "global_custom": global_custom,
    }


def test_map_includes_all_levels_and_project_wins(tmp_path: Path):
    env = _make_env(tmp_path)
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        return_value=[env["stock_base"]],
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path",
        return_value=env["global_table"],
    ):
        mapping = get_footprint_library_map(project_dir=env["project"])

    assert mapping["StockOnly"] == env["stock_base"] / "StockOnly.pretty"
    assert mapping["Seeed_XIAO"] == env["global_custom"]
    assert mapping["AirQuality_Project"] == env["project"] / "AirQuality.pretty"
    # Project table shadows both global table and stock for the same nickname.
    assert mapping["Shared"] == env["project"] / "Shared.pretty"
    # Unresolvable/nonexistent entries are dropped, not propagated.
    assert "Ghost" not in mapping


def test_map_global_wins_over_stock_without_project(tmp_path: Path):
    env = _make_env(tmp_path)
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        return_value=[env["stock_base"]],
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path",
        return_value=env["global_table"],
    ):
        mapping = get_footprint_library_map()

    assert mapping["Shared"] == env["global_shared"]
    # Project-relative entries are absent without a project_dir.
    assert "AirQuality_Project" not in mapping


# ---------------------------------------------------------------------------
# Wiring: _load_kicad_mod + FileLibraryOps resolve table-registered libraries
# ---------------------------------------------------------------------------

FIXTURE_MOD = """\
(footprint "R_Test"
  (layer "F.Cu")
  (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd") (width 0.05))
  (pad "1" smd rect (at -0.9 0) (size 1 1) (layers "F.Cu"))
  (pad "2" smd rect (at 0.9 0) (size 1 1) (layers "F.Cu"))
)
"""


def _make_project_with_lib(tmp_path: Path) -> Path:
    """Project dir holding AirQuality.pretty + fp-lib-table registering it."""
    project = tmp_path / "project"
    lib = project / "AirQuality.pretty"
    lib.mkdir(parents=True)
    (lib / "R_Test.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    (project / "fp-lib-table").write_text(
        "(fp_lib_table\n"
        '  (lib (name AirQuality_Project)(type KiCad)(uri "${KIPRJMOD}/AirQuality.pretty")(options "")(descr ""))\n'
        ")",
        encoding="utf-8",
    )
    return project


def test_load_kicad_mod_resolves_project_table_nickname(tmp_path: Path):
    """The table nickname differs from the .pretty dir name — must still resolve."""
    from kicad_mcp.backends.file_backend import _load_kicad_mod

    project = _make_project_with_lib(tmp_path)
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths", return_value=[]
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path", return_value=None
    ):
        text = _load_kicad_mod("AirQuality_Project:R_Test", project_dir=project)
        assert text is not None
        assert '"R_Test"' in text

        # Without project_dir the nickname is unknown.
        assert _load_kicad_mod("AirQuality_Project:R_Test") is None


def test_load_kicad_mod_global_table(tmp_path: Path):
    """A global-table entry (e.g. Seeed_XIAO) resolves with no project context."""
    from kicad_mcp.backends.file_backend import _load_kicad_mod

    lib = tmp_path / "Seeed_Studio_XIAO_Series.pretty"
    lib.mkdir()
    (lib / "XIAO-ESP32C3-DIP.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    global_table = tmp_path / "fp-lib-table"
    global_table.write_text(
        f'(fp_lib_table (lib (name Seeed_XIAO)(type KiCad)(uri "{lib.as_posix()}")(options "")(descr "")))',
        encoding="utf-8",
    )
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths", return_value=[]
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path",
        return_value=global_table,
    ):
        assert _load_kicad_mod("Seeed_XIAO:XIAO-ESP32C3-DIP") is not None


def test_load_kicad_mod_stem_fallback_still_works(tmp_path: Path):
    """Directories not in any table still resolve by directory-stem match."""
    from kicad_mcp.backends.file_backend import _load_kicad_mod

    stock_base = tmp_path / "footprints"
    lib = stock_base / "Resistor_SMD.pretty"
    lib.mkdir(parents=True)
    (lib / "R_0805.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        return_value=[stock_base],
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path", return_value=None
    ):
        assert _load_kicad_mod("Resistor_SMD:R_0805") is not None


def test_file_library_ops_sees_project_libs(tmp_path: Path):
    from kicad_mcp.backends.file_backend import FileLibraryOps

    project = _make_project_with_lib(tmp_path)
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths", return_value=[]
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path", return_value=None
    ), patch(
        "kicad_mcp.utils.kicad_paths.find_symbol_libraries", return_value=[]
    ):
        ops = FileLibraryOps(project_dir=project)

        libs = ops.list_libraries()
        assert {"name": "AirQuality_Project", "type": "footprint",
                "path": str(project / "AirQuality.pretty")} in libs

        hits = ops.search_footprints("R_Test")
        assert [h["lib_id"] for h in hits] == ["AirQuality_Project:R_Test"]

        info = ops.get_footprint_info("AirQuality_Project:R_Test")
        assert "error" not in info


def test_map_survives_missing_tables(tmp_path: Path):
    stock_base = tmp_path / "footprints"
    (stock_base / "OnlyStock.pretty").mkdir(parents=True)
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        return_value=[stock_base],
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path",
        return_value=None,
    ):
        mapping = get_footprint_library_map(project_dir=tmp_path / "nonexistent")

    assert list(mapping) == ["OnlyStock"]
