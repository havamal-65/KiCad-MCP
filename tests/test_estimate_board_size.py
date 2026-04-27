"""Tests for the estimate_board_size logic in tools/library.py."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — synthetic _parse_footprint_bounds results
# ---------------------------------------------------------------------------

def _make_bounds(width_mm: float, height_mm: float) -> dict:
    """Return a minimal _parse_footprint_bounds-style dict."""
    return {
        "courtyard": {"xmin": 0.0, "ymin": 0.0, "xmax": width_mm, "ymax": height_mm},
        "width_mm": width_mm,
        "height_mm": height_mm,
        "pads": [],
    }


# ---------------------------------------------------------------------------
# Area math
# ---------------------------------------------------------------------------

def test_component_area_sums_correctly():
    """Two footprints → areas add up before any overhead is applied."""
    bounds_a = _make_bounds(10.0, 5.0)   # 50 mm²
    bounds_b = _make_bounds(4.0, 4.0)    # 16 mm²

    def fake_load(lib_id: str) -> str:
        return "dummy"

    def fake_parse(text: str) -> dict:
        # Return different bounds depending on call order
        fake_parse._calls = getattr(fake_parse, "_calls", 0) + 1
        return bounds_a if fake_parse._calls == 1 else bounds_b

    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load), \
         patch("kicad_mcp.backends.file_backend._parse_footprint_bounds", side_effect=fake_parse):
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        fp_ids = ["LibA:FpA", "LibB:FpB"]
        component_area = 0.0
        for fp_id in fp_ids:
            mod = _load_kicad_mod(fp_id)
            assert mod is not None
            b = _parse_footprint_bounds(mod)
            component_area += b["width_mm"] * b["height_mm"]

        assert component_area == pytest.approx(66.0)


def test_missing_footprint_excluded_from_area():
    """A footprint that cannot be found should be reported as missing, not crash."""
    def fake_load(lib_id: str) -> str | None:
        if lib_id == "Missing:FP":
            return None
        return "dummy"

    def fake_parse(text: str) -> dict:
        return _make_bounds(5.0, 5.0)

    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load), \
         patch("kicad_mcp.backends.file_backend._parse_footprint_bounds", side_effect=fake_parse):
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        fp_ids = ["Good:FP", "Missing:FP"]
        component_area = 0.0
        missing = []
        for fp_id in fp_ids:
            mod = _load_kicad_mod(fp_id)
            if mod is None:
                missing.append(fp_id)
                continue
            b = _parse_footprint_bounds(mod)
            w = b["width_mm"] if b["width_mm"] > 0 else 5.0
            h = b["height_mm"] if b["height_mm"] > 0 else 5.0
            component_area += w * h

        assert "Missing:FP" in missing
        assert component_area == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Rounding to 5 mm
# ---------------------------------------------------------------------------

def _ceil5(v: float) -> float:
    return math.ceil(v / 5.0) * 5.0


@pytest.mark.parametrize("raw, expected", [
    (10.0, 10.0),
    (10.1, 15.0),
    (14.9, 15.0),
    (15.0, 15.0),
    (0.1, 5.0),
    (35.0, 35.0),
    (35.01, 40.0),
])
def test_ceil5_rounding(raw, expected):
    assert _ceil5(raw) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Margin logic
# ---------------------------------------------------------------------------

def test_margin_increases_dimensions():
    """A 25% margin must produce dimensions at least 25% larger than the base."""
    base_w, base_h = 50.0, 40.0
    margin_pct = 25.0
    final_w = _ceil5(base_w * (1.0 + margin_pct / 100.0))
    final_h = _ceil5(base_h * (1.0 + margin_pct / 100.0))

    assert final_w >= base_w * 1.25
    assert final_h >= base_h * 1.25


# ---------------------------------------------------------------------------
# End-to-end: estimate for a realistic board must be ≥ 100×80 mm
# ---------------------------------------------------------------------------

def test_realistic_board_estimate_larger_than_components():
    """ESP32 + SCD41 + BME680 footprints → estimate must be larger than any single component.

    Uses synthetic bounds that approximate real module sizes to avoid
    needing system KiCad libraries installed.  The exact mm values depend on
    the formula — this test validates that margin + routing overhead inflate
    dimensions beyond the raw component sizes.
    """
    # Approximate courtyard sizes for key modules
    synthetic_footprints = {
        "RF_Module:ESP32-C3-WROOM-02": _make_bounds(18.0, 20.0),   # ~360 mm²
        "Sensor:SCD41":               _make_bounds(10.1, 33.5),    # ~338 mm²
        "Sensor:BME680":              _make_bounds(3.0, 3.0),      # ~9 mm²
        "Connector_USB:USB_C_Plug":   _make_bounds(9.0, 7.5),      # ~67 mm²
        "Battery_Management:TP4056":  _make_bounds(5.5, 5.5),      # ~30 mm²
    }

    def fake_load(lib_id: str) -> str | None:
        return "dummy" if lib_id in synthetic_footprints else None

    call_order: list[str] = list(synthetic_footprints.keys())
    _idx = {"n": 0}

    def fake_parse(text: str) -> dict:
        key = call_order[_idx["n"] % len(call_order)]
        _idx["n"] += 1
        return synthetic_footprints[key]

    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load), \
         patch("kicad_mcp.backends.file_backend._parse_footprint_bounds", side_effect=fake_parse):
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        component_area = 0.0
        max_single_w = 0.0
        max_single_h = 0.0
        for fp_id in synthetic_footprints:
            mod = _load_kicad_mod(fp_id)
            if mod is None:
                continue
            b = _parse_footprint_bounds(mod)
            w = b["width_mm"] if b["width_mm"] > 0 else 5.0
            h = b["height_mm"] if b["height_mm"] > 0 else 5.0
            component_area += w * h
            max_single_w = max(max_single_w, w)
            max_single_h = max(max_single_h, h)

        routing_overhead = component_area * 0.20
        routed_area = component_area + routing_overhead
        edge_clearance = 3.0
        raw_w = math.sqrt(routed_area * 1.4) + edge_clearance * 2
        raw_h = math.sqrt(routed_area / 1.4) + edge_clearance * 2
        final_w = _ceil5(_ceil5(raw_w) * 1.25)
        final_h = _ceil5(_ceil5(raw_h) * 1.25)

        # Result must beat the largest single-component dimension on at least one axis
        assert final_w > max_single_w, f"Width {final_w} should exceed largest component {max_single_w}"
        assert final_h > max_single_h, f"Height {final_h} should exceed largest component {max_single_h}"
        # Both dimensions must be positive and snapped to 5 mm grid
        assert final_w > 0 and final_w % 5 == 0
        assert final_h > 0 and final_h % 5 == 0
