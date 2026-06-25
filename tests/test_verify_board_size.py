"""Tests for §6.5 verify_board_size + the enhanced estimate_board_size.

Boards are generated in tmp_path with a gr_rect Edge.Cuts outline and footprints
carrying explicit F.CrtYd courtyard rects — the same geometry the production
courtyard parser reads, so no KiCad install is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fastmcp

from kicad_mcp.tools.board import run_verify_board_size
from kicad_mcp.utils.change_log import ChangeLog


def _fp(ref: str, lib: str, ax: float, ay: float,
        courtyard: tuple[float, float] | None) -> str:
    lines = [
        f'  (footprint "{lib}"',
        '    (layer "F.Cu")',
        f'    (at {ax} {ay})',
        f'    (property "Reference" "{ref}" (at 0 0 0))',
    ]
    if courtyard is not None:
        cw, ch = courtyard
        hw, hh = cw / 2, ch / 2
        lines.append(
            f'    (fp_rect (start {-hw} {-hh}) (end {hw} {hh}) '
            f'(layer "F.CrtYd") (width 0.05))'
        )
    lines.append("  )")
    return "\n".join(lines)


def _board(
    tmp_path: Path,
    outline: tuple[float, float] | None,
    parts: list[tuple[str, str, float, float, tuple[float, float] | None]],
    name: str = "b.kicad_pcb",
) -> Path:
    blocks = []
    if outline is not None:
        w, h = outline
        blocks.append(
            f'  (gr_rect (start 0 0) (end {w} {h}) (layer "Edge.Cuts") (width 0.1))'
        )
    blocks.extend(_fp(*p) for p in parts)
    text = "(kicad_pcb (version 20240101) (generator t)\n" + "\n".join(blocks) + "\n)\n"
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ── REQ-TEST-001 — comfortable board passes ──────────────────────────────────

def test_board_comfortably_large_passes(tmp_path: Path):
    board = _board(tmp_path, (60, 50), [
        ("U1", "Lib:A", 15, 15, (5, 5)),
        ("R1", "Lib:R", 30, 15, (5, 5)),
        ("C1", "Lib:C", 45, 15, (5, 5)),
    ])

    result = run_verify_board_size(board)

    assert result["passed"] is True, result
    assert result["parts_counted"] == 3
    assert result["total_required_mm2"] < result["usable_mm2"]


# ── REQ-TEST-002 — area too small fails ──────────────────────────────────────

def test_area_too_small_fails(tmp_path: Path):
    parts = [(f"R{i}", "Lib:R", 10, 10, (5, 5)) for i in range(10)]  # 10*25 = 250 mm²
    board = _board(tmp_path, (20, 20), parts)  # usable inner = 14*14 = 196

    result = run_verify_board_size(board)

    assert result["passed"] is False, result
    sb = result["shortfall_breakdown"]
    assert sb["shortfall_mm2"] > 0
    assert result["suggested_min_dimensions"]["width_mm"] > 20


# ── REQ-TEST-003 — single oversize part fails dimensional check ───────────────

def test_single_part_too_wide_fails(tmp_path: Path):
    # 38 mm wide, 2 mm tall part on a 40x40 board: ample area (76 mm²), but
    # 38 + 2*3 = 44 > 40 — physically cannot fit with the edge keepout band.
    board = _board(tmp_path, (40, 40), [
        ("U1", "Lib:Big", 20, 20, (38, 2)),
    ])

    result = run_verify_board_size(board)

    assert result["passed"] is False, result
    sb = result["shortfall_breakdown"]
    # area itself is fine — failure is dimensional
    assert sb["required_mm2"] <= sb["usable_mm2"], sb
    assert sb["largest_part"]["ref"] == "U1"
    assert sb["largest_part"]["width_mm"] == 38.0


# ── REQ-TEST-004 — no outline ────────────────────────────────────────────────

def test_no_board_outline(tmp_path: Path):
    board = _board(tmp_path, None, [("U1", "Lib:A", 10, 10, (5, 5))])

    result = run_verify_board_size(board)

    assert result["passed"] is False, result
    assert result["shortfall_breakdown"]["reason"] == "no_board_outline"
    assert "outline" in result["message"].lower()


# ── REQ-TEST-007 — high-utilization warning (tight but legal) ─────────────────

def test_high_utilization_warns_but_passes(tmp_path: Path):
    # 30x30 board, usable inner = 24*24 = 576. One 21x20 courtyard = 420 mm²,
    # required = 504 → 504/576 = 0.875 > 0.80 ceiling. Fits dimensionally
    # (30 >= 21+6 and 30 >= 20+6).
    board = _board(tmp_path, (30, 30), [
        ("U1", "Lib:A", 15, 15, (21, 20)),
    ])

    result = run_verify_board_size(board)

    assert result["passed"] is True, result
    hi = [w for w in result["warnings"] if w.get("type") == "high_utilization"]
    assert len(hi) == 1, result["warnings"]


# ── no-courtyard footprint gets a 5x5 default + warning ──────────────────────

def test_footprint_without_courtyard_defaults(tmp_path: Path):
    board = _board(tmp_path, (60, 50), [
        ("U1", "Lib:A", 15, 15, (5, 5)),
        ("TP1", "Lib:TP", 30, 15, None),  # no courtyard
    ])

    result = run_verify_board_size(board)

    assert result["passed"] is True, result
    assert result["parts_counted"] == 2
    nc = [w for w in result["warnings"] if w.get("type") == "no_courtyard"]
    assert len(nc) == 1 and nc[0]["ref"] == "TP1"


# ── REQ-TEST-006 — estimate_board_size round-trips through verify ─────────────

def _estimate_fn(tmp_path: Path):
    from kicad_mcp.tools import library
    mcp = fastmcp.FastMCP("test")
    library.register_tools(mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "estimate_board_size"
    )


def test_estimate_round_trips_through_verify(tmp_path: Path):
    bounds = {"width_mm": 10.0, "height_mm": 10.0, "courtyard": None,
              "pads": [], "npth_pads": [], "min_npth_to_copper_mm": None}
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value="dummy"), \
         patch("kicad_mcp.backends.file_backend._parse_footprint_bounds", return_value=bounds):
        estimate = _estimate_fn(tmp_path)
        est = json.loads(estimate(["Lib:A", "Lib:B", "Lib:C", "Lib:D"]))

    w = est["recommended_width_mm"]
    h = est["recommended_height_mm"]
    # Build a board at the recommended size with those four 10x10 parts placed.
    board = _board(tmp_path, (w, h), [
        (f"U{i}", "Lib:A", 15, 15, (10, 10)) for i in range(4)
    ], name="rt.kicad_pcb")

    result = run_verify_board_size(board)
    assert result["passed"] is True, (est, result)
