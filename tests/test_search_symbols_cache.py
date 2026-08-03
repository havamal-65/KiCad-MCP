"""F5b — search_symbols uses a cheap cached top-level scan.

The scan must return only top-level symbols (not unit sub-symbols) and cache
results per file, keyed by mtime, so repeat searches don't re-read.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from kicad_mcp.backends.file_backend import (
    FileLibraryOps,
    _cached_symbol_names,
    _symbol_name_cache,
    _top_level_symbol_names,
)

LIB = """\
(kicad_symbol_lib
  (version 20250114)
  (generator "kicad_symbol_editor")
  (symbol "R_Small"
    (property "Reference" "R" (at 0 0 0))
    (symbol "R_Small_0_1"
      (rectangle (start 0 0) (end 1 1))
    )
    (symbol "R_Small_1_1"
      (pin passive line (at 0 2.54 270))
    )
  )
  (symbol "C_Small"
    (property "Description" "A cap (with parens) in the string")
    (symbol "C_Small_1_1")
  )
)
"""


def test_top_level_symbol_names_excludes_units() -> None:
    assert _top_level_symbol_names(LIB) == ["R_Small", "C_Small"]


def test_search_symbols_returns_top_level_only(tmp_path: Path) -> None:
    lib = tmp_path / "Device.kicad_sym"
    lib.write_text(LIB, encoding="utf-8")
    _symbol_name_cache.clear()

    with patch(
        "kicad_mcp.utils.kicad_paths.find_symbol_libraries", return_value=[lib]
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_footprint_library_map", return_value={}
    ):
        ops = FileLibraryOps()
        res = ops.search_symbols("Small")

    names = sorted(r["name"] for r in res)
    assert names == ["C_Small", "R_Small"]
    assert all("_0_1" not in n and "_1_1" not in n for n in names)


def test_cache_serves_second_call_and_refreshes_on_change(tmp_path: Path) -> None:
    lib = tmp_path / "Device.kicad_sym"
    lib.write_text(LIB, encoding="utf-8")
    _symbol_name_cache.clear()

    assert _cached_symbol_names(lib) == ["R_Small", "C_Small"]

    # Poison the cache value; an unchanged-mtime call returns the cached list.
    mtime = lib.stat().st_mtime
    _symbol_name_cache[lib] = (mtime, ["CACHED"])
    assert _cached_symbol_names(lib) == ["CACHED"]

    # Rewrite the file with a newer mtime → cache refreshes from disk.
    lib.write_text(LIB.replace("C_Small", "D_Small"), encoding="utf-8")
    os.utime(lib, (mtime + 10, mtime + 10))
    assert _cached_symbol_names(lib) == ["R_Small", "D_Small"]
