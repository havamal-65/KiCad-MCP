"""F5 — symbol-library de-duplication (newest-wins by file stem).

On a multi-version install the system library paths include each version's
``symbols`` directory, so ``find_symbol_libraries`` must return each library
name once (newest wins) rather than once per installed version.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kicad_mcp.utils import kicad_paths


def test_find_symbol_libraries_dedups_by_stem_newest_wins(tmp_path: Path) -> None:
    v10 = tmp_path / "10.0" / "symbols"
    v9 = tmp_path / "9.0" / "symbols"
    v10.mkdir(parents=True)
    v9.mkdir(parents=True)
    for base in (v10, v9):
        (base / "Device.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")
        (base / "power.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")

    # get_system_library_paths yields the newest version's dir first.
    with patch.object(kicad_paths, "get_system_library_paths", return_value=[v10, v9]):
        libs = kicad_paths.find_symbol_libraries()

    assert [p.stem for p in libs] == ["Device", "power"]  # each once, sorted
    # The kept copy is the newest (v10) one.
    for p in libs:
        assert "10.0" in p.parts


def test_find_symbol_libraries_first_dir_wins_stem(tmp_path: Path) -> None:
    """An override 'symbols' dir placed ahead of the versioned ones wins."""
    override = tmp_path / "override" / "symbols"
    versioned = tmp_path / "10.0" / "symbols"
    override.mkdir(parents=True)
    versioned.mkdir(parents=True)
    (override / "Device.kicad_sym").write_text("x", encoding="utf-8")
    (versioned / "Device.kicad_sym").write_text("x", encoding="utf-8")

    with patch.object(
        kicad_paths, "get_system_library_paths", return_value=[override, versioned]
    ):
        libs = kicad_paths.find_symbol_libraries()

    assert len(libs) == 1
    assert "override" in libs[0].parts


def test_find_symbol_libraries_ignores_non_symbols_dirs(tmp_path: Path) -> None:
    symbols = tmp_path / "10.0" / "symbols"
    footprints = tmp_path / "10.0" / "footprints"
    symbols.mkdir(parents=True)
    footprints.mkdir(parents=True)
    (symbols / "Device.kicad_sym").write_text("x", encoding="utf-8")
    # A stray .kicad_sym under a non-"symbols" dir must be skipped.
    (footprints / "Stray.kicad_sym").write_text("x", encoding="utf-8")

    with patch.object(
        kicad_paths, "get_system_library_paths", return_value=[symbols, footprints]
    ):
        libs = kicad_paths.find_symbol_libraries()

    assert [p.stem for p in libs] == ["Device"]
