"""Unit tests for the SQLite-backed parts index."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.parts_index import PartRecord, PartsIndex


@pytest.fixture
def index(tmp_path: Path) -> PartsIndex:
    return PartsIndex(tmp_path / "parts.sqlite")


def _record(**kwargs) -> PartRecord:
    base = {
        "source": "test",
        "mpn": "RC0805FR-0710KL",
        "manufacturer": "Yageo",
        "description": "Resistor 10k 0805 1%",
        "package": "0805",
        "pin_count": 2,
        "value": "10k",
        "symbol_lib_id": "Device:R",
        "footprint_lib_id": "Resistor_SMD:R_0805_2012Metric",
        "datasheet_url": "https://example.com/ds.pdf",
    }
    base.update(kwargs)
    return PartRecord(**base)


class TestUpsert:
    def test_inserts_rows(self, index: PartsIndex):
        n = index.upsert_many([_record(), _record(mpn="ABC123", value="ABC")])
        assert n >= 2
        rows = index.search()
        assert len(rows) == 2

    def test_idempotent_upsert(self, index: PartsIndex):
        index.upsert_many([_record()])
        index.upsert_many([_record(description="updated description")])
        rows = index.search(query="resistor")
        # Same (source, sym_lib_id, fp_lib_id, mpn) tuple → still one row.
        assert len(rows) == 1
        assert rows[0]["description"] == "updated description"

    def test_delete_source_removes_only_that_source(self, index: PartsIndex):
        index.upsert_many([_record(source="kicad")])
        index.upsert_many([_record(source="digikey", mpn="DK-1")])
        deleted = index.delete_source("kicad")
        assert deleted == 1
        remaining = index.search()
        assert len(remaining) == 1
        assert remaining[0]["source"] == "digikey"


class TestSearch:
    def test_query_uses_fts(self, index: PartsIndex):
        index.upsert_many([
            _record(mpn="STM32F407VGT6", manufacturer="STMicro",
                    description="ARM Cortex-M4 MCU", package="LQFP100",
                    pin_count=100, symbol_lib_id="MCU_ST_STM32F4:STM32F407VGT6"),
            _record(mpn="ATmega328P-AU", manufacturer="Microchip",
                    description="8-bit AVR MCU", package="TQFP-32",
                    pin_count=32, symbol_lib_id="MCU_Microchip_ATmega:ATmega328P-AU"),
        ])
        hits = index.search(query="STM32")
        assert len(hits) == 1
        assert "STM32F4" in hits[0]["symbol_lib_id"]

    def test_filter_by_package(self, index: PartsIndex):
        index.upsert_many([
            _record(mpn="A", package="0805"),
            _record(mpn="B", package="QFN-32"),
        ])
        hits = index.search(package="0805")
        assert len(hits) == 1
        assert hits[0]["mpn"] == "A"

    def test_filter_by_pin_range(self, index: PartsIndex):
        index.upsert_many([
            _record(mpn="A", pin_count=2, symbol_lib_id="L:A"),
            _record(mpn="B", pin_count=8, symbol_lib_id="L:B"),
            _record(mpn="C", pin_count=64, symbol_lib_id="L:C"),
        ])
        hits = index.search(min_pins=4, max_pins=16)
        assert [h["mpn"] for h in hits] == ["B"]

    def test_filter_by_source(self, index: PartsIndex):
        index.upsert_many([
            _record(source="kicad-symbols", mpn="K1", symbol_lib_id="L:K1"),
            _record(source="digikey",       mpn="D1", symbol_lib_id="L:D1"),
        ])
        hits = index.search(source="digikey")
        assert {h["source"] for h in hits} == {"digikey"}

    def test_query_with_special_chars_does_not_crash(self, index: PartsIndex):
        index.upsert_many([_record()])
        # Quotes / parens are FTS5 syntax — must be sanitized.
        hits = index.search(query='ABC"(123)*')
        # Should not raise; result list may be empty.
        assert isinstance(hits, list)

    def test_short_query_falls_back_to_unfiltered(self, index: PartsIndex):
        index.upsert_many([_record()])
        # Single-char tokens are dropped; FTS gets an empty pattern that
        # matches nothing — but the call still succeeds.
        hits = index.search(query="x")
        assert isinstance(hits, list)


class TestStats:
    def test_stats_breaks_down_per_source(self, index: PartsIndex):
        index.upsert_many([
            _record(source="kicad", mpn="K1", symbol_lib_id="L:K1"),
            _record(source="kicad", mpn="K2", symbol_lib_id="L:K2"),
            _record(source="digikey", mpn="D1", symbol_lib_id="L:D1"),
        ])
        stats = index.stats()
        assert stats["total"] == 3
        assert stats["per_source"]["kicad"] == 2
        assert stats["per_source"]["digikey"] == 1
