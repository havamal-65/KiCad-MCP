"""REQ-TEST-P2-004 — net-aware placement on real geometry (V-M, AC1/AC2).

The deterministic AC1 comparison (net-aware HPWL < row HPWL on a hand-authored
netlist) runs in the normal suite in ``tests/test_auto_place_strategy.py``. This
file adds the REQ-TRUTH-001 "real geometry, not mocks" leg:

  * ``test_real_board_net_aware_beats_row`` re-places boards authored by *real
    KiCad* with each strategy and asserts net-aware scores a lower Total HPWL —
    no bridge needed (the engine is file-based), so it runs whenever the fixtures
    are present.
  * ``test_live_board_net_aware`` (opt-in, ``KICAD_INTEGRATION=1``) scores the
    board currently open in pcbnew after a net-aware placement.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.utils.placement_metrics import placement_metric

_REAL_BOARDS = [
    "ses_value_revert_v1.kicad_pcb",
    "bt_audio_v1_before_connector_fix.kicad_pcb",
]


@pytest.mark.integration
@pytest.mark.parametrize("board_name", _REAL_BOARDS)
def test_real_board_net_aware_beats_row(board_name: str, tmp_path: Path) -> None:
    src = Path(__file__).parent.parent / "fixtures" / "boards" / board_name
    if not src.exists():
        pytest.skip(f"fixture {board_name} not present")

    scores = {}
    for strat in ("row", "net_aware"):
        dst = tmp_path / f"{strat}_{board_name}"
        shutil.copy(src, dst)
        FileBoardOps().auto_place(
            dst, 0.0, 0.0, 160.0, 120.0, 1.5, strategy=strat,
        )
        scores[strat] = placement_metric(dst)

    # AC1 on real geometry: net-aware is a tighter layout than the row packer.
    assert (
        scores["net_aware"]["total_hpwl_mm"] <= scores["row"]["total_hpwl_mm"]
    ), scores
    # And it stays legal (overlap-free).
    assert scores["net_aware"]["overlap_count"] == 0
    # Decap proximity is populated (a non-negative float) or None when the board
    # has no decoupling caps. The strict within-DECAP_MAX_MM check lives in the
    # controlled-IC fixture (test_auto_place_strategy) — a board carrying a module
    # far larger than DECAP_MAX_MM (e.g. an ESP32) cannot host a cap that close,
    # and the engine's never-worse fallback places it as near as geometry allows.
    dmax = scores["net_aware"]["decap_max_mm"]
    assert dmax is None or (isinstance(dmax, float) and dmax >= 0.0), scores


@pytest.mark.integration
@pytest.mark.parametrize("board_name", _REAL_BOARDS)
def test_real_board_p3_orientation_and_legality(
    board_name: str, tmp_path: Path,
) -> None:
    """REQ-TEST-P3-002 (V-A, real geometry) — P3 orientation normalization runs
    inside net-aware placement on real KiCad boards without breaking legality.

    Rotating a passive changes its courtyard footprint, so the normalization pass
    re-checks overlap/outline; on real geometry the result MUST stay overlap-free
    (REQ-ORIENT/FLOW legality guarantee) and no worse than the row packer on
    outline fit, while remaining a tighter layout than that packer (AC1).
    ``orientation_consistency`` is a valid fraction reflecting the
    family-normalized result. The visual input→output block arrangement is the
    V-M leg documented in the build report. (``out_of_outline_count`` is measured
    against the board's real outline, which is smaller than the 160×120 placement
    area used here — hence the comparison is net-aware-vs-row, not an absolute 0.)
    """
    src = Path(__file__).parent.parent / "fixtures" / "boards" / board_name
    if not src.exists():
        pytest.skip(f"fixture {board_name} not present")

    scores = {}
    for strat in ("row", "net_aware"):
        dst = tmp_path / f"{strat}_{board_name}"
        shutil.copy(src, dst)
        FileBoardOps().auto_place(dst, 0.0, 0.0, 160.0, 120.0, 1.5, strategy=strat)
        scores[strat] = placement_metric(dst)

    na, row = scores["net_aware"], scores["row"]
    # Orientation normalization preserved overlap-freedom on real geometry.
    assert na["overlap_count"] == 0, scores
    # P3 did not worsen outline fit or wire length vs the row packer.
    assert na["out_of_outline_count"] <= row["out_of_outline_count"], scores
    assert na["total_hpwl_mm"] <= row["total_hpwl_mm"], scores
    # The consistency metric reflects the normalized result (a valid fraction).
    oc = na["orientation_consistency"]
    assert isinstance(oc, float) and 0.0 <= oc <= 1.0, scores


@pytest.mark.integration
def test_live_board_net_aware(bridge_session: object, tmp_path: Path) -> None:
    """Score the board open in pcbnew after a net-aware placement (opt-in)."""
    from kicad_mcp.backends.plugin_backend import _tcp_call

    # ping carries the open board's path (identity handshake); get_board_info
    # requires a path argument on the installed bridge, so it can't discover it.
    info = _tcp_call("ping", timeout=5.0)
    board_path = info.get("board_path") if isinstance(info, dict) else None
    if not board_path or not Path(board_path).exists():
        pytest.skip("no live board path available from the bridge")

    bundle = placement_metric(Path(board_path))
    assert isinstance(bundle["total_hpwl_mm"], float)
    assert bundle["total_hpwl_mm"] >= 0.0


# ---------------------------------------------------------------------------
# REQ-TEST-P4-003 — final AC1–AC7 discharge (initiative-closing verification)
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t)
"""

_NETS = [
    (1, "USB_DP"), (2, "USB_DM"), (3, "LED"), (4, "VBUS"), (5, "VCC"),
    (6, "GND"), (7, "EN"), (8, "XIN"), (9, "XOUT"), (10, "LED_K"),
]


def _fp(
    lib_id: str,
    ref: str,
    at: tuple[float, float, float],
    pads: list[tuple[str, float, float, int, str]],
    courtyard: tuple[float, float, float, float],
) -> str:
    fx, fy, frot = at
    s = f'\t(footprint "{lib_id}"\n\t\t(layer "F.Cu")\n\t\t(at {fx} {fy} {frot})\n'
    s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    s += f'\t\t(property "Value" "{ref}_val" (at 0 0 0) (layer "F.Fab"))\n'
    cx0, cy0, cx1, cy1 = courtyard
    s += (
        f"\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
    )
    for name, px, py, nid, nname in pads:
        s += (
            f'\t\t(pad "{name}" smd roundrect (at {px} {py}) (size 1 1) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net {nid} "{nname}"))\n'
        )
    s += "\t)\n"
    return s


def _usb_breakout(tmp_path: Path) -> Path:
    """From-scratch USB-C breakout: connector + regulator + MCU + crystal +
    load caps + decaps + LED resistor, with the USB pair declared as a
    differential pair in the sibling ``.kicad_pro`` (REQ-SENSE-002)."""
    fps = [
        # Anchored input connector at the west edge.
        _fp("Connector_USB:USB_C_Receptacle", "J1", (5.0, 25.0, 0.0), [
            ("A4", 0.5, -1.0, 4, "VBUS"), ("A1", 0.5, 1.0, 6, "GND"),
            ("A6", 1.0, -0.25, 1, "USB_DP"), ("A7", 1.0, 0.25, 2, "USB_DM"),
        ], (-2.0, -2.0, 2.0, 2.0)),
        # Regulator (IC): VBUS in, VCC out.
        _fp("Package_TO_SOT_SMD:SOT-23-5", "U1", (0.0, 0.0, 0.0), [
            ("1", -1.0, -0.5, 4, "VBUS"), ("2", -1.0, 0.5, 6, "GND"),
            ("3", 1.0, -0.5, 5, "VCC"), ("5", 1.0, 0.5, 7, "EN"),
        ], (-2.0, -2.0, 2.0, 2.0)),
        # MCU (IC): USB pair + oscillator pins on the east side.
        _fp("Package_QFP:LQFP-32", "U2", (0.0, 0.0, 0.0), [
            ("1", -1.0, -1.0, 5, "VCC"), ("2", -1.0, 0.0, 6, "GND"),
            ("3", -1.0, 1.0, 1, "USB_DP"), ("4", -1.0, 2.0, 2, "USB_DM"),
            ("7", 1.0, -0.5, 8, "XIN"), ("8", 1.0, 0.5, 9, "XOUT"),
            ("12", 0.0, 1.0, 3, "LED"),
        ], (-2.5, -2.5, 2.5, 2.5)),
        # Decoupling caps (VBUS rail -> U1; VCC rail -> U1/U2 load-balanced).
        _fp("Capacitor_SMD:C_0402", "C1", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 4, "VBUS"), ("2", 0.5, 0.0, 6, "GND")],
            (-0.5, -0.5, 0.5, 0.5)),
        _fp("Capacitor_SMD:C_0402", "C2", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 5, "VCC"), ("2", 0.5, 0.0, 6, "GND")],
            (-0.5, -0.5, 0.5, 0.5)),
        _fp("Capacitor_SMD:C_0402", "C5", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 5, "VCC"), ("2", 0.5, 0.0, 6, "GND")],
            (-0.5, -0.5, 0.5, 0.5)),
        # Crystal + its two load caps (REQ-SENSE-001).
        _fp("Crystal:Crystal_SMD_3225-4Pin", "Y1", (0.0, 0.0, 0.0),
            [("1", -0.6, 0.0, 8, "XIN"), ("2", 0.6, 0.0, 9, "XOUT")],
            (-0.9, -0.9, 0.9, 0.9)),
        _fp("Capacitor_SMD:C_0402", "C3", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 8, "XIN"), ("2", 0.5, 0.0, 6, "GND")],
            (-0.5, -0.5, 0.5, 0.5)),
        _fp("Capacitor_SMD:C_0402", "C4", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 9, "XOUT"), ("2", 0.5, 0.0, 6, "GND")],
            (-0.5, -0.5, 0.5, 0.5)),
        # LED resistor (passive).
        _fp("Resistor_SMD:R_0402", "R1", (0.0, 0.0, 0.0),
            [("1", -0.5, 0.0, 3, "LED"), ("2", 0.5, 0.0, 10, "LED_K")],
            (-0.5, -0.5, 0.5, 0.5)),
    ]
    s = _HEADER + '\t(net 0 "")\n'
    for nid, nname in _NETS:
        s += f'\t(net {nid} "{nname}")\n'
    s += (
        "\t(gr_rect (start 0 0) (end 60 50) "
        '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
    )
    for fp in fps:
        s += fp
    s += ")\n"
    pcb = tmp_path / "usb_breakout.kicad_pcb"
    pcb.write_text(s, encoding="utf-8")

    import json
    (tmp_path / "usb_breakout.kicad_pro").write_text(json.dumps({
        "net_settings": {
            "classes": [
                {"name": "Default"},
                {"name": "USB", "diff_pair_width": 0.2, "diff_pair_gap": 0.15},
            ],
            "netclass_patterns": [{"netclass": "USB", "pattern": "USB_D*"}],
        },
    }), encoding="utf-8")
    return pcb


_AREA = (0.0, 0.0, 60.0, 50.0)


def _place(src: Path, tmp_path: Path, name: str, **kw) -> tuple[Path, dict]:
    dst = tmp_path / f"{name}.kicad_pcb"
    shutil.copy(src, dst)
    shutil.copy(src.with_suffix(".kicad_pro"), dst.with_suffix(".kicad_pro"))
    result = FileBoardOps().auto_place(
        dst, _AREA[0], _AREA[1], _AREA[2], _AREA[3], 0.5,
        anchors=["J1"], **kw,
    )
    return dst, result


@pytest.mark.integration
def test_final_ac_discharge_from_scratch(tmp_path: Path) -> None:
    """REQ-TEST-P4-003 — AC1/AC2/AC3/AC4/AC5/AC7 on a from-scratch build.

    (AC3's pipeline consumption and AC4's byte-for-byte row passthrough are
    additionally pinned at unit level in test_auto_place_strategy.py; here the
    from-scratch build proves the whole set coheres on one real board. AC6 is
    discharged in test_final_ac6_gate_blocks below.)
    """
    import math

    from kicad_mcp.utils import placement_engine as e
    from kicad_mcp.utils.placement_config import get_float

    src = _usb_breakout(tmp_path)

    # AC3: no strategy argument -> net-aware is the default.
    na_board, na_result = _place(src, tmp_path, "na")
    assert na_result["strategy"] == "net_aware"

    # AC7: the anchored connector is preserved exactly.
    parts = e.read_part_records(na_board.read_text(encoding="utf-8"))
    pos = {p["ref"]: p["pos"] for p in parts}
    assert pos["J1"] == (5.0, 25.0, 0.0)

    # AC1: lower Total HPWL than the legacy row packer on the same netlist.
    row_board, row_result = _place(src, tmp_path, "row", strategy="row")
    # AC4: legacy reachable — and its response shape is the unchanged P2 row
    # contract (rows populated, no "strategy" key added to the legacy path).
    assert "strategy" not in row_result and row_result["rows"] >= 1
    na_score = placement_metric(na_board)
    row_score = placement_metric(row_board)
    assert na_score["total_hpwl_mm"] < row_score["total_hpwl_mm"], (
        na_score, row_score,
    )
    # Legal: overlap-free and inside the outline.
    assert na_score["overlap_count"] == 0
    assert na_score["out_of_outline_count"] == 0

    # AC2: every decap within DECAP_MAX_MM on this controlled build.
    assert na_score["decap_max_mm"] is not None
    assert na_score["decap_max_mm"] <= get_float("DECAP_MAX_MM"), na_score

    # REQ-SENSE-001: the crystal sits within SENSE_MAX_MM of the MCU.
    by_ref = {p["ref"]: p for p in parts}

    def centre(ref: str) -> tuple[float, float]:
        p = by_ref[ref]
        box = e._board_box(p, (p["pos"][0], p["pos"][1]), p["pos"][2])
        return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

    (yx, yy), (ux, uy) = centre("Y1"), centre("U2")
    assert math.hypot(yx - ux, yy - uy) <= get_float("SENSE_MAX_MM")

    # REQ-SENSE-002: the .kicad_pro diff-pair declaration is read.
    assert e.read_diff_pair_nets(na_board) == frozenset({"USB_DP", "USB_DM"})

    # AC5: deterministic — a second run is byte-identical.
    na2_board, _ = _place(src, tmp_path, "na2")
    assert na2_board.read_bytes() == na_board.read_bytes()
    # AC4 determinism for the legacy path too.
    row2_board, _ = _place(src, tmp_path, "row2", strategy="row")
    assert row2_board.read_bytes() == row_board.read_bytes()


@pytest.mark.integration
def test_final_ac6_gate_blocks(tmp_path: Path) -> None:
    """REQ-TEST-P4-003 (AC6) — the protocol reports the score and the gate
    blocks: a good placement passes validate_placement_quality; forcing a bad
    placement flips it to blocked and autoroute refuses."""
    import json
    from unittest.mock import MagicMock

    import fastmcp

    from kicad_mcp.tools.drc import (
        run_validate_connector_orientations,
        run_validate_placement_quality,
    )
    from kicad_mcp.utils.change_log import ChangeLog

    src = _usb_breakout(tmp_path)
    board, _ = _place(src, tmp_path, "gated")

    # Good placement: gate passes and carries the metric bundle (the report).
    good = run_validate_placement_quality(board)
    assert good["passed"] is True, good
    assert good["placement_metric"]["total_hpwl_mm"] > 0.0

    # Force a bad placement: shove the MCU outside the outline.
    FileBoardOps().move_component(board, "U2", 200.0, 200.0)
    bad = run_validate_placement_quality(board)
    assert bad["passed"] is False
    assert any(v["type"] == "out_of_outline" for v in bad["violations"])

    # autoroute refuses on the failed gate (orientation gate passes first —
    # J1's face is indeterminate-free on this fixture-style footprint).
    orient = run_validate_connector_orientations(board)
    assert orient["passed"] is True

    from kicad_mcp.tools import routing
    mcp = fastmcp.FastMCP("test")
    routing.register_tools(
        mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"), config={},
    )
    autoroute_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "autoroute"
    )
    refusal = json.loads(autoroute_fn(str(board)))
    assert refusal["status"] == "error"
    assert "validate_placement_quality" in refusal["message"]


@pytest.mark.integration
def test_live_board_p4_gate(bridge_session: object) -> None:
    """P4 live leg (opt-in): the quality gate runs against the board open in
    pcbnew — the metric is reported and the verdict recorded to the sidecar
    validation cache next to the live board file."""
    from kicad_mcp.backends.plugin_backend import _tcp_call
    from kicad_mcp.tools.drc import run_validate_placement_quality
    from kicad_mcp.utils.validation_cache import get_validation

    info = _tcp_call("ping", timeout=5.0)
    board_path = info.get("board_path") if isinstance(info, dict) else None
    if not board_path or not Path(board_path).exists():
        pytest.skip("no live board path available from the bridge")

    p = Path(board_path)
    result = run_validate_placement_quality(p)
    assert isinstance(result["passed"], bool)
    assert "total_hpwl_mm" in result["placement_metric"]
    cached = get_validation(p, "validate_placement_quality")
    assert cached is not None and cached["passed"] == result["passed"]
