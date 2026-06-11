"""REQ-COV-006/007/008/009 — component placement and movement handlers.

Each test owns a refdes prefix (T06_*/T07_*/T08_*/T09_*) per REQ-ISO-001
so concurrent or partial runs don't collide. Tests do their own setup
and don't depend on each other (REQ-ISO-002).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration

_RESISTOR_FP = "Resistor_SMD:R_0805_2012Metric"
_POSITION_TOL_MM = 0.001  # 1 µm


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def _find_component(components: list[dict], reference: str) -> dict | None:
    for comp in components:
        if comp.get("reference") == reference:
            return comp
    return None


def _find_at_in_file(board_path: str, reference: str) -> tuple[float, float, float] | None:
    """Read the saved .kicad_pcb and return the (x, y, rot) from the footprint
    block matching *reference*. Returns None if not found. Rotation defaults
    to 0.0 when the `(at x y)` block omits it.
    """
    text = Path(board_path).read_text(encoding="utf-8")
    # Find each (footprint ...) block; check if its Reference property matches.
    fp_iter = re.finditer(r"\(footprint\s+", text)
    for match in fp_iter:
        start = match.start()
        # Find matching close paren by walking depth.
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    break
            i += 1
        else:
            continue
        if not re.search(rf'\(property\s+"Reference"\s+"{re.escape(reference)}"', block):
            continue
        # Footprint-level (at x y [rot]) — the first one inside this block.
        at_match = re.search(r"\(at\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)(?:\s+(-?\d+\.?\d*))?\s*\)", block)
        if at_match is None:
            return None
        x = float(at_match.group(1))
        y = float(at_match.group(2))
        rot = float(at_match.group(3)) if at_match.group(3) else 0.0
        return x, y, rot
    return None


def test_place_component_appears_in_get_components(bridge_session):
    """REQ-COV-006: place a single component, observe it in get_components."""
    path = _board_path()
    ref = "T06_R1"
    _tcp_call(
        "place_component", 5.0,
        path=path, reference=ref, footprint=_RESISTOR_FP,
        x=10.0, y=10.0, rotation=0,
    )
    components = _tcp_call("get_components", 5.0, path=path)
    found = _find_component(components, ref)
    assert found is not None, f"{ref} not found in get_components after placement"
    assert abs(found["x"] - 10.0) < _POSITION_TOL_MM, found
    assert abs(found["y"] - 10.0) < _POSITION_TOL_MM, found


def test_place_components_bulk_adds_all(bridge_session):
    """REQ-COV-007: place three components in one bulk call, observe all three."""
    path = _board_path()
    refs = ["T07_R1", "T07_R2", "T07_R3"]
    positions = [(30.0, 10.0), (32.0, 10.0), (34.0, 10.0)]
    payload = [
        {"reference": r, "footprint": _RESISTOR_FP, "x": x, "y": y, "rotation": 0}
        for r, (x, y) in zip(refs, positions)
    ]
    result = _tcp_call("place_components_bulk", 10.0, path=path, components=payload)

    assert isinstance(result, dict), f"bulk response not a dict: {result!r}"
    assert sorted(result.get("placed", [])) == sorted(refs), \
        f"placed list mismatch: {result.get('placed')!r} vs {refs!r}, failed={result.get('failed')!r}"
    assert result.get("failed") == [], f"bulk reported failures: {result.get('failed')!r}"

    components = _tcp_call("get_components", 5.0, path=path)
    for ref, (x, y) in zip(refs, positions):
        comp = _find_component(components, ref)
        assert comp is not None, f"{ref} not found in get_components after bulk"
        assert abs(comp["x"] - x) < _POSITION_TOL_MM, comp
        assert abs(comp["y"] - y) < _POSITION_TOL_MM, comp


def test_move_component_translation(bridge_session):
    """REQ-COV-008: place then move; new (x, y) reflected in get_components AND saved file."""
    path = _board_path()
    ref = "T08_R1"
    _tcp_call(
        "place_component", 5.0,
        path=path, reference=ref, footprint=_RESISTOR_FP,
        x=50.0, y=50.0, rotation=0,
    )
    # Now translate.
    _tcp_call(
        "move_component", 5.0,
        path=path, reference=ref, x=55.0, y=52.0,
    )
    # get_components reflects the move.
    comp = _find_component(_tcp_call("get_components", 5.0, path=path), ref)
    assert comp is not None, f"{ref} disappeared after move"
    assert abs(comp["x"] - 55.0) < _POSITION_TOL_MM, comp
    assert abs(comp["y"] - 52.0) < _POSITION_TOL_MM, comp
    # Saved file reflects the move.
    at = _find_at_in_file(path, ref)
    assert at is not None, f"{ref} not found in saved file"
    saved_x, saved_y, _ = at
    assert abs(saved_x - 55.0) < _POSITION_TOL_MM, at
    assert abs(saved_y - 52.0) < _POSITION_TOL_MM, at


def test_move_component_rotation(bridge_session):
    """REQ-COV-009: rotate via move_component; new rotation in saved file."""
    path = _board_path()
    ref = "T09_R1"
    _tcp_call(
        "place_component", 5.0,
        path=path, reference=ref, footprint=_RESISTOR_FP,
        x=70.0, y=50.0, rotation=0,
    )
    _tcp_call(
        "move_component", 5.0,
        path=path, reference=ref, x=70.0, y=50.0, rotation=90,
    )
    at = _find_at_in_file(path, ref)
    assert at is not None, f"{ref} not found in saved file"
    _, _, rot = at
    assert abs(rot - 90.0) < 0.01, f"expected rotation 90, got {rot}"
