"""auto_place strategy dispatch (Sprint P2).

  * REQ-TEST-P2-002 — net-aware yields a strictly lower total_hpwl_mm than the
    row packer on the same netlist (the P1 metric is the judge).
  * REQ-TEST-P2-backcompat / REQ-BACK-001 — strategy="row" is a pure passthrough
    to the unchanged row packer (byte-identical placements).
  * REQ-API-005 — an unknown strategy is a structured error, nothing mutated.
  * Production routing — PluginBoardOps applies net-aware via the existing bridge
    move_component (no new bridge handler); row still forwards to the bridge.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.placement_metrics import placement_metric


# ---------------------------------------------------------------------------
# A board with real nets + courtyards in the footprint blocks, so the engine
# reads geometry straight from the board (no library lookup / no mock).
# ---------------------------------------------------------------------------

_HEADER = (
    "(kicad_pcb\n"
    "\t(version 20241229)\n"
    '\t(generator "pcbnew")\n'
    '\t(layers (0 "F.Cu" signal) (31 "F.CrtYd" user "F.Courtyard")'
    ' (25 "Edge.Cuts" user))\n'
    '\t(net 0 "")\n'
    '\t(net 1 "IN0")(net 2 "IN1")(net 3 "OUT0")(net 4 "OUT1")'
    '(net 5 "VCC")(net 6 "GND")\n'
    "\t(gr_rect (start -5 -5) (end 120 90) (stroke (width 0.1) (type solid))"
    ' (fill no) (layer "Edge.Cuts"))\n'
)


def _fp(ref: str, lib: str, x: float, y: float, pads, cy) -> str:
    s = (
        f'\t(footprint "{lib}" (layer "F.Cu") (at {x} {y} 0)\n'
        f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'\t\t(property "Value" "{ref}v" (at 0 0 0) (layer "F.Fab"))\n'
        f"\t\t(fp_rect (start {cy[0]} {cy[1]}) (end {cy[2]} {cy[3]})"
        ' (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
    )
    for pn, nid, nm, dx, dy in pads:
        s += (
            f'\t\t(pad "{pn}" smd roundrect (at {dx} {dy}) (size 0.3 0.3)'
            f' (layers "F.Cu") (net {nid} "{nm}"))\n'
        )
    return s + "\t)\n"


def _netlist_board() -> str:
    body = _HEADER
    body += _fp("U1", "SOIC8", 0, 0, [
        ("1", 1, "IN0", -1, 0), ("2", 2, "IN1", -0.5, 0), ("4", 6, "GND", 0, 0),
        ("5", 3, "OUT0", 0.5, 0), ("6", 4, "OUT1", 1, 0), ("8", 5, "VCC", -1, 1),
    ], (-2, -2, 2, 2))
    body += _fp("J1", "CONN2", 0, 0,
                [("1", 1, "IN0", 0, 0), ("2", 2, "IN1", 1, 0)], (-1, -1, 1, 1))
    body += _fp("J2", "CONN2", 0, 0,
                [("1", 3, "OUT0", 0, 0), ("2", 4, "OUT1", 1, 0)], (-1, -1, 1, 1))
    body += _fp("C1", "C0402", 0, 0,
                [("1", 5, "VCC", -0.5, 0), ("2", 6, "GND", 0.5, 0)],
                (-0.5, -0.5, 0.5, 0.5))
    body += _fp("C2", "C0402", 0, 0,
                [("1", 5, "VCC", -0.5, 0), ("2", 6, "GND", 0.5, 0)],
                (-0.5, -0.5, 0.5, 0.5))
    return body + ")\n"


def test_net_aware_beats_row_on_hpwl(tmp_path: Path) -> None:
    """REQ-TEST-P2-002 / AC1: net-aware HPWL < row HPWL on the same netlist."""
    board = _netlist_board()
    metrics = {}
    for strat in ("row", "net_aware"):
        p = tmp_path / f"{strat}.kicad_pcb"
        p.write_text(board, encoding="utf-8")
        FileBoardOps().auto_place(
            p, 0.0, 0.0, 110.0, 80.0, 1.5, strategy=strat,
        )
        metrics[strat] = placement_metric(p)
    assert metrics["net_aware"]["total_hpwl_mm"] < metrics["row"]["total_hpwl_mm"]
    # Net-aware keeps it legal too.
    assert metrics["net_aware"]["overlap_count"] == 0


def test_net_aware_decaps_within_max(tmp_path: Path) -> None:
    """AC2: every decoupling cap is within DECAP_MAX_MM of its IC."""
    from kicad_mcp.utils.placement_config import DECAP_MAX_MM

    p = tmp_path / "na.kicad_pcb"
    p.write_text(_netlist_board(), encoding="utf-8")
    FileBoardOps().auto_place(p, 0.0, 0.0, 110.0, 80.0, 1.5, strategy="net_aware")
    bundle = placement_metric(p)
    assert bundle["decap_max_mm"] is not None
    assert bundle["decap_max_mm"] <= DECAP_MAX_MM


def test_row_strategy_is_pure_passthrough(tmp_path: Path) -> None:
    """REQ-BACK-001: strategy='row' == the unchanged row packer, byte-identical."""
    board = _netlist_board()
    p1 = tmp_path / "via_dispatch.kicad_pcb"
    p2 = tmp_path / "direct.kicad_pcb"
    p1.write_text(board, encoding="utf-8")
    p2.write_text(board, encoding="utf-8")

    via_dispatch = FileBoardOps().auto_place(
        p1, 0.0, 0.0, 110.0, 80.0, 1.5, strategy="row",
    )
    direct = FileBoardOps()._auto_place_row(
        p2, 0.0, 0.0, 110.0, 80.0, 1.5,
    )
    assert via_dispatch == direct
    assert p1.read_text(encoding="utf-8") == p2.read_text(encoding="utf-8")


def test_default_strategy_is_net_aware() -> None:
    """AC3: the default strategy is net_aware (pcb_pipeline/build-pcb get it free)."""
    import inspect

    sig = inspect.signature(FileBoardOps.auto_place)
    assert sig.parameters["strategy"].default == "net_aware"


# ---------------------------------------------------------------------------
# Tool-level validation
# ---------------------------------------------------------------------------

def test_unknown_strategy_is_structured_error(tmp_path: Path) -> None:
    """REQ-API-005: unknown strategy → error, nothing touched (gate not reached)."""
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    backend = MagicMock()

    mcp = fastmcp.FastMCP("test")
    from kicad_mcp.tools import board
    board.register_tools(mcp, backend, ChangeLog(tmp_path / "c.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "auto_place"
    )
    result = json.loads(tool_fn(str(p), strategy="bogus"))
    assert result["status"] == "error"
    assert "net_aware" in result["message"] and "row" in result["message"]
    backend.get_board_modify_ops.assert_not_called()


# ---------------------------------------------------------------------------
# Production routing — PluginBoardOps (mock the TCP bridge boundary)
# ---------------------------------------------------------------------------

def test_plugin_row_forwards_to_bridge(tmp_path: Path) -> None:
    from kicad_mcp.backends.plugin_backend import PluginBoardOps

    p = tmp_path / "b.kicad_pcb"
    p.write_text(_netlist_board(), encoding="utf-8")
    ops = PluginBoardOps()
    calls: list[str] = []

    def fake_call(cmd, path, **kw):
        calls.append(cmd)
        return {"placed": [], "warnings": [], "count": 0}

    ops._call = fake_call  # type: ignore[assignment]
    ops.auto_place(p, 0.0, 0.0, 110.0, 80.0, 1.5, strategy="row")
    # Row goes straight to the bridge auto_place handler, no per-part moves.
    assert calls == ["auto_place"]


def test_plugin_net_aware_plans_and_moves_via_bridge(tmp_path: Path) -> None:
    """Net-aware on the plugin: plan server-side, apply through move_component."""
    from kicad_mcp.backends.plugin_backend import PluginBoardOps

    p = tmp_path / "b.kicad_pcb"
    p.write_text(_netlist_board(), encoding="utf-8")
    ops = PluginBoardOps()
    moved: list[tuple[str, float, float]] = []

    def fake_call(cmd, path, **kw):
        if cmd == "move_component":
            moved.append((kw["reference"], kw["x"], kw["y"]))
        return {"status": "success"}

    ops._call = fake_call  # type: ignore[assignment]
    result = ops.auto_place(p, 0.0, 0.0, 110.0, 80.0, 1.5, strategy="net_aware")

    assert result["strategy"] == "net_aware"
    # Every non-anchor part was applied via a bridge move_component call.
    assert result["components_placed"] == 5
    assert {m[0] for m in moved} == {"U1", "J1", "J2", "C1", "C2"}
