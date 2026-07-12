"""Unit tests for the shared duplicate-reference placement guard (F2 REQ-DUP).

Spec: known-issues-remediation-2/placement-integrity §1, test plan §4.
"""

from __future__ import annotations

import inspect

import pytest

from kicad_mcp.backends.placement_guard import (
    POSITION_TOL_MM,
    DuplicateRefError,
    ExistingComponent,
    check_placement,
    existing_from_component,
    find_batch_duplicate_refs,
    idempotent_success,
    index_existing,
)


def _existing(**overrides) -> ExistingComponent:
    base = dict(
        reference="R1",
        lib_id="Resistor_SMD:R_0805_2012Metric",
        x=10.0,
        y=20.0,
        rotation=0.0,
        layer="F.Cu",
    )
    base.update(overrides)
    return ExistingComponent(**base)


# ---------------------------------------------------------------------------
# REQ-DUP-1 — new ref creates
# ---------------------------------------------------------------------------

def test_new_ref_creates():
    assert check_placement(
        None, "R1", "Resistor_SMD:R_0805_2012Metric", 10.0, 20.0,
    ) == "create"


# ---------------------------------------------------------------------------
# REQ-DUP-2 — idempotent match (pos within tol, rotation mod 360, same layer)
# ---------------------------------------------------------------------------

def test_idempotent_exact_match():
    assert check_placement(
        _existing(), "R1", "Resistor_SMD:R_0805_2012Metric", 10.0, 20.0,
        req_rot=0.0, req_layer="F.Cu",
    ) == "idempotent"


def test_idempotent_within_position_tolerance():
    # 0.01 mm off < POSITION_TOL_MM — a sexp float round-trip, not a move.
    assert check_placement(
        _existing(), "R1", "Resistor_SMD:R_0805_2012Metric",
        10.0 + 0.01, 20.0 - 0.01,
    ) == "idempotent"


def test_idempotent_just_inside_tolerance():
    # The exact boundary is float-representation territory; the contract that
    # matters is just-inside passes, just-outside refuses (next test block).
    assert check_placement(
        _existing(), "R1", "Resistor_SMD:R_0805_2012Metric",
        10.0 + POSITION_TOL_MM * 0.99, 20.0,
    ) == "idempotent"


def test_rotation_matches_mod_360():
    assert check_placement(
        _existing(rotation=90.0), "R1", "Resistor_SMD:R_0805_2012Metric",
        10.0, 20.0, req_rot=450.0,
    ) == "idempotent"
    assert check_placement(
        _existing(rotation=-270.0), "R1", "Resistor_SMD:R_0805_2012Metric",
        10.0, 20.0, req_rot=90.0,
    ) == "idempotent"


# ---------------------------------------------------------------------------
# REQ-DUP-3 — informative refusal on any differing placement
# ---------------------------------------------------------------------------

def test_differing_position_refuses_with_existing_state():
    with pytest.raises(DuplicateRefError) as exc_info:
        check_placement(
            _existing(), "R1", "Resistor_SMD:R_0805_2012Metric", 30.0, 20.0,
        )
    err = exc_info.value
    assert err.existing.x == 10.0
    assert err.suggested_tool == "move_component"
    assert "R1" in str(err)


def test_position_just_past_tolerance_refuses():
    with pytest.raises(DuplicateRefError):
        check_placement(
            _existing(), "R1", "Resistor_SMD:R_0805_2012Metric",
            10.0 + POSITION_TOL_MM * 2, 20.0,
        )


def test_differing_rotation_refuses():
    # Review clarification 2026-07-11: same position but rotated is NOT
    # idempotent — "success" would silently keep the old orientation.
    with pytest.raises(DuplicateRefError):
        check_placement(
            _existing(rotation=0.0), "R1", "Resistor_SMD:R_0805_2012Metric",
            10.0, 20.0, req_rot=90.0,
        )


def test_differing_layer_refuses():
    with pytest.raises(DuplicateRefError):
        check_placement(
            _existing(layer="F.Cu"), "R1", "Resistor_SMD:R_0805_2012Metric",
            10.0, 20.0, req_layer="B.Cu",
        )


def test_differing_footprint_suggests_swap():
    with pytest.raises(DuplicateRefError) as exc_info:
        check_placement(
            _existing(), "R1", "Capacitor_SMD:C_0603_1608Metric", 10.0, 20.0,
        )
    assert "footprint swap" in exc_info.value.suggested_tool


def test_refusal_payload_shape():
    with pytest.raises(DuplicateRefError) as exc_info:
        check_placement(
            _existing(), "R1", "Resistor_SMD:R_0805_2012Metric", 30.0, 40.0,
        )
    payload = exc_info.value.to_refusal()
    assert payload["status"] == "refused"
    assert payload["existing"] == {
        "reference": "R1",
        "footprint": "Resistor_SMD:R_0805_2012Metric",
        "x": 10.0,
        "y": 20.0,
        "rotation": 0.0,
        "layer": "F.Cu",
    }
    assert payload["suggested_tool"] == "move_component"
    assert "duplicate" in payload["reason"]


def test_idempotent_success_payload():
    payload = idempotent_success(_existing())
    assert payload["status"] == "success"
    assert payload["idempotent"] is True
    assert payload["reference"] == "R1"


# ---------------------------------------------------------------------------
# REQ-DUP-6 — no force/opt-out flag anywhere in the guard API
# ---------------------------------------------------------------------------

def test_no_force_flag():
    params = inspect.signature(check_placement).parameters
    assert not any(
        "force" in name or "allow" in name or "override" in name
        for name in params
    )


# ---------------------------------------------------------------------------
# Adapters — both backend record shapes resolve identically
# ---------------------------------------------------------------------------

def test_existing_from_bridge_flat_record():
    comp = {
        "reference": "U1",
        "footprint": "Package_QFP:LQFP-48",
        "x": 50.0,
        "y": 60.0,
        "layer": "B.Cu",
        "rotation": 90.0,
        "value": "MCU",
    }
    existing = existing_from_component(comp)
    assert existing == ExistingComponent(
        reference="U1", lib_id="Package_QFP:LQFP-48",
        x=50.0, y=60.0, rotation=90.0, layer="B.Cu",
    )


def test_existing_from_file_nested_record():
    # File backend: nested position dict; rotation absent when zero in sexp.
    comp = {
        "reference": "C3",
        "footprint": "Capacitor_SMD:C_0603_1608Metric",
        "position": {"x": 12.5, "y": 8.25},
        "layer": "F.Cu",
    }
    existing = existing_from_component(comp)
    assert existing.x == 12.5
    assert existing.y == 8.25
    assert existing.rotation == 0.0


def test_index_existing_first_occurrence_wins():
    # On an already-duplicated board, the guard must refuse against the same
    # copy other by-ref tools resolve to (the first).
    comps = [
        {"reference": "R1", "footprint": "A:B", "x": 1.0, "y": 1.0},
        {"reference": "R1", "footprint": "A:B", "x": 99.0, "y": 99.0},
        {"reference": "R2", "footprint": "A:B", "x": 2.0, "y": 2.0},
    ]
    index = index_existing(comps)
    assert index["R1"].x == 1.0
    assert set(index) == {"R1", "R2"}


# ---------------------------------------------------------------------------
# REQ-DUP-4 — in-batch duplicate detection (whole batch refused by callers)
# ---------------------------------------------------------------------------

def test_bulk_in_batch_dup_detected():
    comps = [
        {"reference": "R1", "footprint": "A:B"},
        {"reference": "R2", "footprint": "A:B"},
        {"reference": "R1", "footprint": "A:B"},
        {"reference": "R2", "footprint": "A:B"},
    ]
    assert find_batch_duplicate_refs(comps) == ["R1", "R2"]


def test_bulk_clean_batch_has_no_dupes():
    comps = [
        {"reference": "R1", "footprint": "A:B"},
        {"reference": "R2", "footprint": "A:B"},
    ]
    assert find_batch_duplicate_refs(comps) == []


def test_bulk_blank_refs_not_counted_as_dupes():
    # Blank refs are a per-item "missing reference" failure, not an
    # in-batch-duplicate batch refusal.
    comps = [
        {"reference": "", "footprint": "A:B"},
        {"reference": "", "footprint": "A:B"},
    ]
    assert find_batch_duplicate_refs(comps) == []
