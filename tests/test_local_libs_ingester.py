"""Unit tests for the local-libs ingester (regex-based .kicad_sym/.pretty walker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.parts_index import PartsIndex
from kicad_mcp.utils.source_ingesters.local_libs import LocalLibsIngester


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def index(tmp_path: Path) -> PartsIndex:
    return PartsIndex(tmp_path / "parts.sqlite")


@pytest.fixture
def registry(tmp_path: Path) -> LibrarySourceRegistry:
    """A fresh registry that also includes the on-disk test fixtures.

    The fixtures dir has separate ``symbols/`` and ``footprints/``
    children; we register each under a distinct name so we can test
    source filtering.
    """
    reg = LibrarySourceRegistry(registry_path=tmp_path / "registry.json")
    reg.register("symbols_fixture", str(FIXTURES / "symbols"), source_type="local")
    reg.register("footprints_fixture", str(FIXTURES / "footprints"), source_type="local")
    return reg


class TestLocalLibsIngester:
    def test_indexes_symbols_from_kicad_sym(
        self, index: PartsIndex, registry: LibrarySourceRegistry,
    ):
        ingester = LocalLibsIngester(index, "symbols_fixture", registry=registry)
        result = ingester.ingest()
        assert result.indexed > 0
        assert result.errors == [] or all("No .kicad_sym" not in e for e in result.errors)

        # Device:R should be indexed with pin_count=2.
        rows = index.search(query="Device", source="symbols_fixture")
        names = {r["symbol_lib_id"] for r in rows}
        assert "Device:R" in names
        r_row = next(r for r in rows if r["symbol_lib_id"] == "Device:R")
        assert r_row["pin_count"] == 2

    def test_indexes_footprints_from_pretty(
        self, index: PartsIndex, registry: LibrarySourceRegistry,
    ):
        ingester = LocalLibsIngester(index, "footprints_fixture", registry=registry)
        result = ingester.ingest()
        assert result.indexed > 0

        rows = index.search(query="0805", source="footprints_fixture")
        ids = {r["footprint_lib_id"] for r in rows}
        assert any(lib_id and "R_0805_2012Metric" in lib_id for lib_id in ids)

    def test_package_heuristic_from_footprint_name(
        self, index: PartsIndex, registry: LibrarySourceRegistry,
    ):
        LocalLibsIngester(index, "footprints_fixture", registry=registry).ingest()
        # The 0805 fixture should be flagged with package=0805 even
        # though the .kicad_mod has no explicit package property.
        hits = index.search(package="0805", source="footprints_fixture")
        assert any(r["package"] == "0805" for r in hits)

    def test_reingest_replaces_existing_rows(
        self, index: PartsIndex, registry: LibrarySourceRegistry,
    ):
        ingester = LocalLibsIngester(index, "symbols_fixture", registry=registry)
        first = ingester.ingest().indexed
        second = ingester.ingest().indexed
        # Same fixtures → same row count after a full delete+reinsert pass.
        assert first == second
        # And there's only one row per (source, lib_id, mpn) tuple.
        all_rows = index.search(source="symbols_fixture", limit=1000)
        ids = [r["symbol_lib_id"] for r in all_rows]
        assert len(ids) == len(set(ids))

    def test_unknown_source_returns_error(
        self, index: PartsIndex, tmp_path: Path,
    ):
        empty_reg = LibrarySourceRegistry(registry_path=tmp_path / "empty.json")
        ingester = LocalLibsIngester(index, "no-such-source", registry=empty_reg)
        result = ingester.ingest()
        assert result.indexed == 0
        assert any("No .kicad_sym or .pretty" in e for e in result.errors)
