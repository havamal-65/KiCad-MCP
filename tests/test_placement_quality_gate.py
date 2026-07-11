"""REQ-TEST-P4-002 — the placement-quality gate (Sprint P4).

Covers the gate policy (blocking overlaps / out-of-outline, advisory HPWL and
decap-distance promotable via GATE_PROMOTE_ADVISORY), the validation-cache
recording that makes it a first-class gate, the autoroute refusal, and the MCP
tool surface. Boards are tiny hand-authored ``.kicad_pcb`` strings in
``tmp_path`` — no KiCad installation required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from kicad_mcp.tools.drc import run_validate_placement_quality
from kicad_mcp.utils import placement_config
from kicad_mcp.utils.gates import check_gate
from kicad_mcp.utils.validation_cache import get_validation

# ---------------------------------------------------------------------------
# Minimal board builder (mirrors test_placement_metrics.py)
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


def _pad(name: str, px: float, py: float, nid: int, nname: str) -> str:
    return (
        f'\t\t(pad "{name}" smd roundrect (at {px} {py}) (size 1 1) '
        f'(layers "F.Cu" "F.Mask" "F.Paste") (net {nid} "{nname}"))\n'
    )


def _footprint(
    lib_id: str,
    ref: str,
    at: tuple[float, float, float],
    pads: list[tuple[str, float, float, int, str]],
    courtyard: tuple[float, float, float, float] | None = (-2.0, -2.0, 2.0, 2.0),
) -> str:
    fx, fy, frot = at
    s = f'\t(footprint "{lib_id}"\n'
    s += '\t\t(layer "F.Cu")\n'
    s += f"\t\t(at {fx} {fy} {frot})\n"
    s += f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    s += f'\t\t(property "Value" "{ref}_val" (at 0 0 0) (layer "F.Fab"))\n'
    if courtyard is not None:
        cx0, cy0, cx1, cy1 = courtyard
        s += (
            f"\t\t(fp_rect (start {cx0} {cy0}) (end {cx1} {cy1}) "
            '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        )
    for pad in pads:
        s += _pad(*pad)
    s += "\t)\n"
    return s


def _board(
    footprints: list[str],
    nets: list[tuple[int, str]],
    outline: tuple[float, float, float, float] | None = (0.0, 0.0, 60.0, 60.0),
) -> str:
    s = _HEADER
    s += '\t(net 0 "")\n'
    for nid, nname in nets:
        s += f'\t(net {nid} "{nname}")\n'
    if outline is not None:
        x0, y0, x1, y1 = outline
        s += (
            f"\t(gr_rect (start {x0} {y0}) (end {x1} {y1}) "
            '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
        )
    for fp in footprints:
        s += fp
    s += ")\n"
    return s


def _write(tmp_path: Path, content: str, name: str = "b.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


_NETS = [(1, "SIG"), (2, "IN0"), (3, "IN1"), (5, "VCC"), (6, "GND")]


def _overlap_board(tmp_path: Path) -> Path:
    """Two 4x4 courtyards 1 mm apart — one courtyard overlap."""
    fps = [
        _footprint("LibA:U", "R1", (10.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
        _footprint("LibA:U", "R2", (11.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    return _write(tmp_path, _board(fps, _NETS))


def _out_of_outline_board(tmp_path: Path) -> Path:
    """One courtyard poking past the east edge of the 60x60 outline."""
    fps = [
        _footprint("LibA:U", "R1", (59.0, 10.0, 0.0), [("1", 0.0, 0.0, 1, "SIG")]),
    ]
    return _write(tmp_path, _board(fps, _NETS))


def _clean_board_with_far_decap(tmp_path: Path) -> Path:
    """Legal placement whose decap sits ~42 mm from its IC (advisory-only) and
    whose one signal net spans 20 mm (HPWL 20)."""
    fps = [
        _footprint("LibA:MCU", "U1", (10.0, 10.0, 0.0), [
            ("1", 0.0, 0.0, 1, "SIG"), ("2", 0.5, 0.0, 2, "IN0"),
            ("3", 1.0, 0.0, 3, "IN1"), ("4", -0.5, 0.0, 5, "VCC"),
            ("5", -1.0, 0.0, 6, "GND"),
        ]),
        _footprint("LibA:R", "R1", (30.0, 10.0, 0.0),
                   [("1", 0.0, 0.0, 1, "SIG")],
                   courtyard=(-0.5, -0.5, 0.5, 0.5)),
        _footprint("LibA:C", "C1", (40.0, 40.0, 0.0),
                   [("1", -0.5, 0.0, 5, "VCC"), ("2", 0.5, 0.0, 6, "GND")],
                   courtyard=(-0.5, -0.5, 0.5, 0.5)),
    ]
    return _write(tmp_path, _board(fps, _NETS))


# ---------------------------------------------------------------------------
# Gate policy (REQ-GATE-001)
# ---------------------------------------------------------------------------

def test_gate_blocks_on_courtyard_overlap(tmp_path: Path) -> None:
    p = _overlap_board(tmp_path)
    result = run_validate_placement_quality(p)
    assert result["passed"] is False
    kinds = {v["type"]: v for v in result["violations"]}
    assert kinds["courtyard_overlap"]["severity"] == "blocking"
    assert kinds["courtyard_overlap"]["count"] >= 1
    assert result["required_actions"]
    # The failure is a first-class gate: check_gate reports ran + not passed.
    gap = check_gate(p, "validate_placement_quality")
    assert gap == {
        "ran": True, "passed": False, "violations": result["violations"],
    }


def test_gate_blocks_on_out_of_outline(tmp_path: Path) -> None:
    p = _out_of_outline_board(tmp_path)
    result = run_validate_placement_quality(p)
    assert result["passed"] is False
    kinds = {v["type"] for v in result["violations"]}
    assert "out_of_outline" in kinds
    assert "courtyard_overlap" not in kinds


def test_gate_passes_clean_with_advisory_nonblocking(tmp_path: Path) -> None:
    """A far decap is an advisory violation only — the gate still passes and
    check_gate opens (Q5 lean: HPWL/decap advisory by default)."""
    p = _clean_board_with_far_decap(tmp_path)
    result = run_validate_placement_quality(p)
    assert result["passed"] is True
    advisory = [v for v in result["violations"] if v["severity"] == "advisory"]
    assert any(v["type"] == "decap_distance_exceeds_target" for v in advisory)
    assert not any(v["severity"] == "blocking" for v in result["violations"])
    assert check_gate(p, "validate_placement_quality") is None
    # The metric bundle rides along for the phase report.
    assert result["placement_metric"]["total_hpwl_mm"] == 20.0


def test_gate_hpwl_ceiling_advisory_by_default(tmp_path: Path, monkeypatch) -> None:
    """A configured GATE_HPWL_MAX_MM below the board's HPWL yields an advisory
    violation that does not block (REQ-GATE-001)."""
    p = _clean_board_with_far_decap(tmp_path)
    monkeypatch.setattr(
        placement_config, "load_overrides", lambda: {"GATE_HPWL_MAX_MM": 1.0},
    )
    result = run_validate_placement_quality(p)
    assert result["passed"] is True
    hpwl = [v for v in result["violations"] if v["type"] == "hpwl_exceeds_budget"]
    assert hpwl and hpwl[0]["severity"] == "advisory"
    assert hpwl[0]["total_hpwl_mm"] == 20.0 and hpwl[0]["budget_mm"] == 1.0


def test_gate_promotion_makes_advisory_blocking(tmp_path: Path, monkeypatch) -> None:
    """GATE_PROMOTE_ADVISORY=True turns advisory violations into a hard fail
    without code edits (REQ-CFG-002)."""
    p = _clean_board_with_far_decap(tmp_path)
    monkeypatch.setattr(
        placement_config, "load_overrides",
        lambda: {"GATE_HPWL_MAX_MM": 1.0, "GATE_PROMOTE_ADVISORY": True},
    )
    result = run_validate_placement_quality(p)
    assert result["passed"] is False
    assert check_gate(p, "validate_placement_quality") is not None
    assert any("promoted to blocking" in a for a in result["required_actions"])


def test_gate_records_validation_cache(tmp_path: Path) -> None:
    """The result lands in <board>.validation_cache.json (REQ-GATE-002)."""
    p = _clean_board_with_far_decap(tmp_path)
    assert get_validation(p, "validate_placement_quality") is None
    run_validate_placement_quality(p)
    cached = get_validation(p, "validate_placement_quality")
    assert cached is not None and cached["passed"] is True
    # Any content change invalidates the pass (hash-keyed cache).
    p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert get_validation(p, "validate_placement_quality") is None


# ---------------------------------------------------------------------------
# Autoroute enforcement (REQ-GATE-001 via gates.check_gate)
# ---------------------------------------------------------------------------

def _call_autoroute(board_path: Path) -> dict:
    """Drive autoroute through the registered MCP tool with a mocked backend.

    Only the gates run before any backend call, so no FreeRouting is needed.
    """
    import fastmcp
    from kicad_mcp.tools import routing
    from kicad_mcp.utils.change_log import ChangeLog

    backend_stub = MagicMock()
    # No live path in this harness: clean_board_for_routing must fall through
    # to its headless disk script, not be "served" by the mock (S2 row 18).
    backend_stub.get_board_modify_ops.side_effect = NotImplementedError
    change_log = ChangeLog(board_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    routing.register_tools(mcp, backend_stub, change_log, config={})
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "autoroute"
    )
    return json.loads(tool_fn(str(board_path)))


def _pass_orientation_gate(board_path: Path) -> None:
    from kicad_mcp.tools.drc import run_validate_connector_orientations

    val = run_validate_connector_orientations(board_path)
    assert val["passed"] is True  # no connectors on these fixtures


def test_autoroute_refuses_when_quality_gate_unrun(tmp_path: Path) -> None:
    p = _clean_board_with_far_decap(tmp_path)
    _pass_orientation_gate(p)
    result = _call_autoroute(p)
    assert result["status"] == "error"
    assert "validate_placement_quality" in result["message"]
    assert "has not been run" in result["message"]
    assert any(
        s["step"] == "placement_quality_gate" and s["status"] == "error"
        for s in result["steps"]
    )


def test_autoroute_refuses_when_quality_gate_failed(tmp_path: Path) -> None:
    p = _overlap_board(tmp_path)
    _pass_orientation_gate(p)
    val = run_validate_placement_quality(p)
    assert val["passed"] is False  # sanity
    result = _call_autoroute(p)
    assert result["status"] == "error"
    assert "blocking violations" in result["message"]
    assert result["violations"]


def test_autoroute_proceeds_past_quality_gate_when_passed(tmp_path: Path) -> None:
    """Both gates green → the quality-gate step reports success; downstream may
    still fail (no FreeRouting in CI) — the gate is what this test owns."""
    p = _clean_board_with_far_decap(tmp_path)
    _pass_orientation_gate(p)
    assert run_validate_placement_quality(p)["passed"] is True
    result = _call_autoroute(p)
    assert any(
        s["step"] == "placement_quality_gate" and s["status"] == "success"
        for s in result["steps"]
    )


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_validate_placement_quality_tool_surface(tmp_path: Path) -> None:
    import fastmcp
    from kicad_mcp.tools import drc
    from kicad_mcp.utils.change_log import ChangeLog

    p = _overlap_board(tmp_path)
    mcp = fastmcp.FastMCP("test")
    drc.register_tools(mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "validate_placement_quality"
    )
    result = json.loads(tool_fn(str(p)))
    assert result["status"] == "success"
    assert result["passed"] is False
    assert result["placement_metric"]["overlap_count"] >= 1
    assert result["required_actions"]
