"""Regression tests for the schematic symbol-pin rotation transform (#17 residual).

Audits ``FileSchematicOps.get_symbol_pin_positions`` against eeschema's own pin
placement for every rotation (0/90/180/270) and mirror (none/X/Y) combination.

Ground truth is captured in ``tests/fixtures/rotation_audit/`` and was proven
equal to eeschema by a headless ``kicad-cli sch erc`` no-connect round-trip
(see the fixture README and ``tests/integration/test_symbol_pin_rotation_live``).
This mirrors the board-domain ``test_rotation_convention.py``: values come from
KiCad itself, not from the code under test.

The bug being pinned: the previous transposed rotation matrix
(``px*cos - py*sin / px*sin + py*cos`` with a pre-rotation mirror) agreed with
eeschema only at 0/180 degrees and for mirrored 90/270, but reflected pins
through the symbol origin at un-mirrored 90/270 -- the schematic twin of the
board rotation bug fixed in b65df77.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rotation_audit"
_SCH = _FIXTURE_DIR / "rotation_audit.kicad_sch"
_GROUND_TRUTH: dict = json.loads(
    (_FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8")
)

# eeschema stores schematic coordinates on a 0.0001 mm grid; the transform
# rounds to 4 places and pin positions are grid-snapped, so an exact match is
# expected. 1e-3 mm absorbs any float noise while still catching the mm-scale
# reflection the old transposed transform produced.
_TOL = 1e-3


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _TOL


@pytest.mark.parametrize("ref", sorted(_GROUND_TRUTH))
def test_pin_positions_match_eeschema(ref: str) -> None:
    """Every pin of every orientation matches eeschema's ground truth."""
    ops = FileSchematicOps()
    result = ops.get_symbol_pin_positions(_SCH, ref)
    assert "error" not in result, result
    computed = result["pin_positions"]
    expected = _GROUND_TRUTH[ref]["pins"]

    assert set(computed) == set(expected), (
        f"{ref}: pin set mismatch {set(computed)} != {set(expected)}"
    )
    for pin, (ex, ey) in expected.items():
        cx, cy = computed[pin]["x"], computed[pin]["y"]
        assert _close(cx, ex) and _close(cy, ey), (
            f"{ref} pin {pin}: computed ({cx}, {cy}) != eeschema ({ex}, {ey})"
        )


# Independent anchors: absolute pin positions computed by hand from KiCad's
# TRANSFORM (eeschema/sch_symbol.cpp) for the two orientations the old transform
# got wrong, and confirmed against the kicad-cli ERC oracle. These are hard-coded
# (not read from ground_truth.json) so the test fails on a reverted fix even if
# ground_truth.json is regenerated from broken code -- exactly how
# test_rotation_convention.py hard-codes pcbnew-observed pad positions.
#
# LM7805_TO220 library pins (Y-up): p1 (-7.62, 0), p2 (0, -7.62), p3 (7.62, 0).
# Correct rotation of a Y-flipped point: 90deg -> (-py, -px); 270deg -> (py, px).
_EESCHEMA_ANCHORS = {
    # U90N origin (127.0, 50.8), 90 deg, no mirror
    "U90N": {"1": (127.0, 58.42), "2": (134.62, 50.8), "3": (127.0, 43.18)},
    # U270N origin (76.2, 101.6), 270 deg, no mirror
    "U270N": {"1": (76.2, 93.98), "2": (68.58, 101.6), "3": (76.2, 109.22)},
}


@pytest.mark.parametrize("ref", sorted(_EESCHEMA_ANCHORS))
def test_bug_orientations_match_hardcoded_eeschema(ref: str) -> None:
    """The formerly-broken un-mirrored 90/270 cases match eeschema exactly."""
    ops = FileSchematicOps()
    computed = ops.get_symbol_pin_positions(_SCH, ref)["pin_positions"]
    for pin, (ex, ey) in _EESCHEMA_ANCHORS[ref].items():
        cx, cy = computed[pin]["x"], computed[pin]["y"]
        assert _close(cx, ex) and _close(cy, ey), (
            f"{ref} pin {pin}: computed ({cx}, {cy}) != eeschema ({ex}, {ey})"
        )


def test_ground_truth_covers_all_twelve_orientations() -> None:
    """Guard against a truncated fixture silently shrinking coverage."""
    assert len(_GROUND_TRUTH) == 12
    expected_refs = {
        f"U{a:.0f}{m}"
        for a in (0, 90, 180, 270)
        for m in ("N", "X", "Y")
    }
    assert set(_GROUND_TRUTH) == expected_refs
    assert sum(len(v["pins"]) for v in _GROUND_TRUTH.values()) == 36
