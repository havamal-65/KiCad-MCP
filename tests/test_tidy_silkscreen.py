"""FD (KIR3 S4) — silkscreen tidy: relocate-first, hide-as-fallback.

Two layers, both KiCad-free:
- pure helpers (classifier, ring search, warning→refdes mapping, silk tally)
- the ``tidy_silkscreen`` tool, driving a mock backend whose DRC + reference-text
  ops are scripted (mock at the ops boundary, per src/CLAUDE.md).

The pcbnew-side bridge handlers (`_handle_set_reference_text` etc.) run inside
KiCad's embedded Python and are verified live, not here; this file only checks
their dispatch registration.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_mcp.tools import board as board_tools
from kicad_mcp.tools.board import (
    _boxes_clear,
    _find_clear_spot,
    _implicated_refs,
    _is_small_passive,
    _plan_silk_tidy,
    _ref_prefix,
    _silk_counts,
)
from kicad_mcp.utils.change_log import ChangeLog

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_ref_prefix():
    assert _ref_prefix("R5") == "R"
    assert _ref_prefix("LED12") == "LED"
    assert _ref_prefix("U1") == "U"
    assert _ref_prefix("") == ""


def test_is_small_passive_allows_small_r_c():
    # Real silk-inclusive 0603 body bbox from the live probe: 3.01 × 1.51 mm.
    # The narrow axis (1.51) is what qualifies it.
    assert _is_small_passive("R5", 3.01, 1.51, 2.0) is True
    assert _is_small_passive("D3", 3.40, 1.59, 2.0) is True   # 0603 LED


def test_is_small_passive_rejects_ic_and_connector():
    assert _is_small_passive("U1", 0.5, 0.5, 2.0) is False  # IC never hidden
    assert _is_small_passive("J2", 0.5, 0.5, 2.0) is False  # connector never hidden


def test_is_small_passive_rejects_oversized():
    # SOD-123 diode narrow axis 2.35 > 2.0 → not hidden (kept labeled).
    assert _is_small_passive("D1", 4.79, 2.35, 2.0) is False
    # A 1206 chip reads ~2.2 mm on its narrow axis → also excluded.
    assert _is_small_passive("R9", 3.4, 2.2, 2.0) is False


def test_is_small_passive_denylist_beats_size():
    # RN (resistor network) is on the denylist even though R is allowed and it's small.
    assert _is_small_passive("RN1", 3.01, 1.51, 2.0) is False


def test_boxes_clear_separated_and_touching():
    a = (0.0, 0.0, 1.0, 1.0)
    assert _boxes_clear(a, (2.0, 0.0, 3.0, 1.0), 0.15) is True   # gap 1.0
    assert _boxes_clear(a, (1.1, 0.0, 2.0, 1.0), 0.15) is False  # gap 0.1 < 0.15
    assert _boxes_clear(a, (0.5, 0.5, 1.5, 1.5), 0.15) is False  # overlapping


def test_find_clear_spot_finds_gap():
    # Footprint body at origin, only its own box as obstacle → a spot above is clear.
    fp = (0.0, 0.0, 2.0, 2.0)
    spot = _find_clear_spot(1.0, 0.5, fp, [fp], 0.15)
    assert spot is not None


def test_find_clear_spot_returns_none_when_boxed_in():
    fp = (0.0, 0.0, 2.0, 2.0)
    # A huge obstacle covering the whole search radius leaves nowhere clear.
    spot = _find_clear_spot(1.0, 0.5, fp, [(-100.0, -100.0, 100.0, 100.0)], 0.15)
    assert spot is None


def test_find_clear_spot_stays_on_board():
    # Live-caught case (aqs_v3 C6): part hugs the west board edge — without the
    # board constraint the west candidate wins and the text hangs off the edge,
    # trading silk_overlap for silk_edge_clearance forever. With board_rect the
    # chosen spot's text box must sit fully on the board with clearance.
    board = (100.0, 80.0, 170.0, 130.0)
    fp = (100.2, 92.0, 103.2, 93.5)      # flush against x=100 west edge
    spot = _find_clear_spot(2.2, 1.7, fp, [fp], 0.15, board_rect=board)
    assert spot is not None
    x, y = spot
    assert x - 2.2 / 2.0 >= 100.0 + 0.15   # text box fully on-board (west)
    assert y - 1.7 / 2.0 >= 80.0 + 0.15    # and north


def test_find_clear_spot_unconstrained_without_board_rect():
    # Back-compat: with no board_rect the same setup may use the off-board
    # west spot (this is the pre-fix behavior the constraint exists to stop).
    fp = (100.2, 92.0, 103.2, 93.5)
    spot = _find_clear_spot(2.2, 1.7, fp, [fp], 0.15)
    assert spot is not None


def test_implicated_refs_reference_field_primary():
    # Real KiCad-10 wording: the movable refdes is "Reference field of C7"; the
    # paired item is U3's PAD. Only C7 (the field owner) is relocatable — U3 must
    # not be attributed even though it is a known ref in the description.
    v = {"type": "silk_over_copper",
         "items": [{"description": "Reference field of C7"},
                   {"description": "Pad 4 [Net-(U3-4)] of U3 on F.Cu"}]}
    assert _implicated_refs(v, {"C7", "U3"}) == ["C7"]


def test_implicated_refs_reference_field_overlap_pair():
    # Two reference fields overlapping → both are relocatable.
    v = {"type": "silk_overlap",
         "items": [{"description": "Reference field of R3"},
                   {"description": "Reference field of R4"}]}
    assert _implicated_refs(v, {"R3", "R4"}) == ["R3", "R4"]


def test_implicated_refs_never_matches_text_label_pseudofootprints():
    # Real wording from the KiCad-10 cm5_minima demo: KiBuzzard text-label
    # footprints carry a Reference field that IS the placed artwork. Their
    # non-annotation reference shape must never be attributed — even when the
    # pseudo-ref is present in known_refs (it is: labels appear in the bulk
    # reference-text snapshot like any footprint).
    v = {"type": "silk_over_copper",
         "items": [{"description": "Reference field of kibuzzard-6773B9F7"},
                   {"description": "Pad 2 [Net-(R605-Pad2)] of R605 on F.Cu"}]}
    assert _implicated_refs(v, {"kibuzzard-6773B9F7", "R605"}) == []


def test_implicated_refs_body_silk_only_returns_empty():
    # Two body-silk segments overlapping, no reference field → nothing this tool
    # can relocate; must not misattribute to the segment owners.
    v = {"type": "silk_overlap",
         "items": [{"description": "Segment of D2 on F.Silkscreen"},
                   {"description": "Polygon of U3 on F.Silkscreen"}]}
    assert _implicated_refs(v, {"D2", "U3"}) == []


def test_silk_counts():
    drc = {"violations": [
        {"type": "silk_over_copper"},
        {"type": "silk_overlap"},
        {"type": "silk_over_copper"},
        {"type": "shorting_items"},   # not a silk type
    ]}
    counts = _silk_counts(drc)
    assert counts["total"] == 3
    assert counts["by_type"]["silk_over_copper"] == 2
    assert counts["by_type"]["silk_overlap"] == 1


# ── plan (module-level, exercised against real geometry too) ──────────────────

def _prec(ref, ref_x, ref_y, fp):
    x0, y0, x1, y1 = fp
    return {"reference": ref, "visible": True, "layer": "F.SilkS",
            "ref_x": ref_x, "ref_y": ref_y, "ref_w": 2.2, "ref_h": 1.6,
            "fp_x0": x0, "fp_y0": y0, "fp_x1": x1, "fp_y1": y1}


def _pviol(ref):
    return {"type": "silk_over_copper",
            "items": [{"description": f"Reference field of {ref}"},
                      {"description": "Pad 1 [NET] of X9 on F.Cu"}]}


def test_plan_relocates_on_sparse_board():
    # Two well-separated parts → both refdes relocate, nothing hidden/skipped.
    recs = [_prec("R1", 10.0, 10.0, (9.0, 9.0, 11.0, 11.0)),
            _prec("C1", 30.0, 30.0, (29.0, 29.0, 31.0, 31.0))]
    plan = _plan_silk_tidy(recs, [_pviol("R1"), _pviol("C1")], "auto", 0.15, 2.0)
    assert sorted(r["ref"] for r in plan["relocated"]) == ["C1", "R1"]
    assert plan["hidden"] == [] and plan["skipped"] == []


def test_plan_hide_fallback_when_boxed_in():
    # R1 (chip passive) fully ringed by a large NB body beyond the ring-search
    # radius → no clear spot → auto hides it.
    recs = [_prec("R1", 20.0, 20.0, (19.7, 19.7, 20.3, 20.3)),
            _prec("NB", 20.0, 20.0, (0.0, 0.0, 40.0, 40.0))]
    plan = _plan_silk_tidy(recs, [_pviol("R1")], "auto", 0.15, 2.0)
    assert plan["relocated"] == [] and plan["hidden"] == ["R1"]


def test_plan_respects_board_rect():
    # Part at the west edge: with board_rect the planned spot keeps the text
    # box on the board; the plan still relocates (north/south instead of west).
    recs = [_prec("C6", 101.5, 92.8, (100.2, 92.0, 103.2, 93.5))]
    plan = _plan_silk_tidy(recs, [_pviol("C6")], "auto", 0.15, 2.0,
                           board_rect=(100.0, 80.0, 170.0, 130.0))
    assert [r["ref"] for r in plan["relocated"]] == ["C6"]
    to_x, to_y = plan["relocated"][0]["to"]
    assert to_x - 2.2 / 2.0 >= 100.0 + 0.15


def test_plan_ignores_non_silk_violations():
    recs = [_prec("R1", 10.0, 10.0, (9.0, 9.0, 11.0, 11.0))]
    noise = {"type": "shorting_items", "items": [{"description": "Reference field of R1"}]}
    plan = _plan_silk_tidy(recs, [noise], "auto", 0.15, 2.0)
    assert plan["implicated"] == []


# ── bridge dispatch registration ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("kicad_mcp_bridge_silk_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_registers_reference_text_handlers(bridge):
    for key in ("get_reference_text", "get_reference_texts", "set_reference_text"):
        assert key in bridge._DISPATCH


def test_bridge_set_reference_text_is_mutating(bridge):
    # write path gets the stale-board coherence pre-check; the reads do not
    assert "set_reference_text" in bridge._MUTATING_METHODS
    assert "get_reference_texts" not in bridge._MUTATING_METHODS


def test_bridge_version_bumped(bridge):
    assert bridge._BRIDGE_VERSION == "2.8.0-tracemaker"


# ── tool integration ──────────────────────────────────────────────────────────

def _rec(ref, ref_x, ref_y, fp, ref_w=1.0, ref_h=0.5, visible=True):
    x0, y0, x1, y1 = fp
    return {
        "reference": ref, "visible": visible, "layer": "F.SilkS",
        "ref_x": ref_x, "ref_y": ref_y, "ref_w": ref_w, "ref_h": ref_h,
        "fp_x0": x0, "fp_y0": y0, "fp_x1": x1, "fp_y1": y1,
    }


def _silk(vtype, ref):
    return {"type": vtype, "description": "Silkscreen clipped by solder mask",
            "severity": "warning",
            "items": [{"description": f"Reference field of {ref}"},
                      {"description": "Pad 1 [NET] of NB on F.Cu"}]}


def _board_file(tmp_path: Path) -> Path:
    f = tmp_path / "b.kicad_pcb"
    f.write_text("(kicad_pcb (version 20241229))", encoding="utf-8")
    return f


def _make_backend(records, before, after):
    ops = MagicMock()
    ops.get_reference_texts.return_value = {"references": records}
    ops.set_reference_text.return_value = {"status": "ok"}
    drc = MagicMock()
    drc.run_drc.side_effect = [before, after]
    backend = MagicMock()
    backend.get_board_modify_ops.return_value = ops
    backend.get_drc_ops.return_value = drc
    return backend, ops, drc


def _tidy_fn(backend, change_log):
    import fastmcp
    mcp = fastmcp.FastMCP("test")
    board_tools.register_tools(mcp, backend, change_log)
    return next(t.fn for t in mcp._tool_manager._tools.values()
                if t.name == "tidy_silkscreen")


def test_tidy_relocate_mode_moves_and_never_hides(tmp_path: Path):
    p = _board_file(tmp_path)
    records = [
        _rec("R1", 10.0, 10.0, (9.0, 9.0, 11.0, 11.0)),
        _rec("U1", 30.0, 30.0, (27.0, 27.0, 33.0, 33.0)),
    ]
    before = {"violations": [_silk("silk_over_copper", "R1")],
              "error_count": 0, "warning_count": 1}
    after = {"violations": [], "error_count": 0, "warning_count": 0}
    backend, ops, _ = _make_backend(records, before, after)

    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="relocate"))

    assert out["status"] == "success"
    assert [r["ref"] for r in out["relocated"]] == ["R1"]
    assert out["hidden"] == []
    # a relocate move was issued for R1, none hidden
    move_calls = [c for c in ops.set_reference_text.call_args_list
                  if c.kwargs.get("visible") is None]
    assert len(move_calls) == 1
    assert out["silk_before"]["total"] == 1
    assert out["silk_after"]["total"] == 0


def test_tidy_hide_mode_hides_small_passives_only(tmp_path: Path):
    p = _board_file(tmp_path)
    records = [
        _rec("R1", 10.0, 10.0, (9.7, 9.7, 10.3, 10.3)),   # small passive
        _rec("U1", 30.0, 30.0, (27.0, 27.0, 33.0, 33.0)),  # IC — never hidden
    ]
    before = {"violations": [_silk("silk_over_copper", "R1"),
                             _silk("silk_over_copper", "U1")],
              "error_count": 0, "warning_count": 2}
    after = {"violations": [], "error_count": 0, "warning_count": 0}
    backend, ops, _ = _make_backend(records, before, after)

    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="hide"))

    assert out["hidden"] == ["R1"]
    assert out["relocated"] == []
    assert any(s["ref"] == "U1" for s in out["skipped"])  # IC left in place
    hide_calls = [c for c in ops.set_reference_text.call_args_list
                  if c.kwargs.get("visible") is False]
    assert [c.args[1] for c in hide_calls] == ["R1"]


def test_tidy_auto_falls_back_to_hide_when_no_clear_spot(tmp_path: Path):
    p = _board_file(tmp_path)
    # R1 (small passive) is fully ringed by NB's body → no clear relocate spot,
    # so auto mode falls back to hiding it. NB itself is not implicated.
    records = [
        _rec("R1", 10.0, 10.0, (9.7, 9.7, 10.3, 10.3)),
        _rec("NB", 10.0, 10.0, (5.0, 5.0, 15.0, 15.0)),
    ]
    before = {"violations": [_silk("silk_over_copper", "R1")],
              "error_count": 0, "warning_count": 1}
    after = {"violations": [], "error_count": 0, "warning_count": 0}
    backend, ops, _ = _make_backend(records, before, after)

    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="auto"))

    assert out["relocated"] == []
    assert out["hidden"] == ["R1"]


def test_tidy_dry_run_issues_no_writes(tmp_path: Path):
    p = _board_file(tmp_path)
    records = [_rec("R1", 10.0, 10.0, (9.0, 9.0, 11.0, 11.0))]
    before = {"violations": [_silk("silk_over_copper", "R1")],
              "error_count": 0, "warning_count": 1}
    after = {"violations": [], "error_count": 0, "warning_count": 0}
    backend, ops, drc = _make_backend(records, before, after)

    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="auto", dry_run=True))

    assert out["dry_run"] is True
    assert out["applied"] is False
    assert [r["ref"] for r in out["relocated"]] == ["R1"]   # planned, not applied
    ops.set_reference_text.assert_not_called()
    # only the baseline DRC ran, not an after-DRC
    assert drc.run_drc.call_count == 1


def test_tidy_rejects_bad_mode(tmp_path: Path):
    p = _board_file(tmp_path)
    backend, _, _ = _make_backend([], {"violations": []}, {"violations": []})
    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="squish"))
    assert out["status"] == "error"
    assert "mode" in out["message"]


def test_tidy_reports_residual_and_zero_errors(tmp_path: Path):
    p = _board_file(tmp_path)
    records = [_rec("R1", 10.0, 10.0, (9.7, 9.7, 10.3, 10.3)),
               _rec("NB", 10.0, 10.0, (5.0, 5.0, 15.0, 15.0))]
    before = {"violations": [_silk("silk_over_copper", "R1")],
              "error_count": 0, "warning_count": 1}
    # After hiding R1, a mask-clip warning persists (KiCad #15052).
    after = {"violations": [_silk("silk_over_copper", "R1")],
             "error_count": 0, "warning_count": 1}
    backend, _, _ = _make_backend(records, before, after)

    fn = _tidy_fn(backend, ChangeLog(tmp_path / "c.json"))
    out = json.loads(fn(str(p), mode="auto"))

    assert out["hidden"] == ["R1"]
    assert out["drc_errors_after"] == 0
    assert any(r["type"] == "silk_over_copper" for r in out["residual_warnings"])
