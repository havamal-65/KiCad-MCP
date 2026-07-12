"""Wiring tests: the #16 duplicate-ref guard in every place path (F2 REQ-DUP).

Covers the file backend, the bridge client (PluginBoardOps — guard runs
client-side so the installed bridge needs no change), and the tool layer's
structured refusal. The IPC path's guard is tested in test_ipc_backend.py
with the kipy fakes; the pure rule itself in test_placement_guard.py.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.backends.placement_guard import (
    DuplicateRefError,
    ExistingComponent,
)
from kicad_mcp.backends.plugin_backend import PluginBoardOps


# ---------------------------------------------------------------------------
# File backend — place_component
# ---------------------------------------------------------------------------

def test_file_place_new_ref_appends(tmp_board: Path):
    ops = FileBoardOps()
    result = ops.place_component(tmp_board, "R9", "DoesNotExist:Stub", 5.0, 5.0)
    assert result["reference"] == "R9"
    refs = [c["reference"] for c in ops.get_components(tmp_board)]
    assert refs.count("R9") == 1


def test_file_place_idempotent_no_duplicate(tmp_board: Path):
    # R1 exists as "Device:R" at (100, 100) rot 0 on F.Cu — an identical
    # re-place (the retry-after-timeout scenario) succeeds without appending.
    ops = FileBoardOps()
    before = tmp_board.read_text(encoding="utf-8")
    result = ops.place_component(tmp_board, "R1", "Device:R", 100.0, 100.0)
    assert result["status"] == "success"
    assert result["idempotent"] is True
    assert tmp_board.read_text(encoding="utf-8") == before  # board untouched
    refs = [c["reference"] for c in ops.get_components(tmp_board)]
    assert refs.count("R1") == 1


def test_file_place_differing_position_refuses_untouched(tmp_board: Path):
    ops = FileBoardOps()
    before = tmp_board.read_text(encoding="utf-8")
    with pytest.raises(DuplicateRefError) as exc_info:
        ops.place_component(tmp_board, "R1", "Device:R", 50.0, 50.0)
    assert exc_info.value.suggested_tool == "move_component"
    assert exc_info.value.existing.x == 100.0
    assert tmp_board.read_text(encoding="utf-8") == before


def test_file_place_differing_footprint_refuses(tmp_board: Path):
    ops = FileBoardOps()
    with pytest.raises(DuplicateRefError) as exc_info:
        ops.place_component(tmp_board, "R1", "Device:C", 100.0, 100.0)
    assert "swap" in exc_info.value.suggested_tool


# ---------------------------------------------------------------------------
# File backend — place_components_bulk
# ---------------------------------------------------------------------------

def test_file_bulk_in_batch_dup_refuses_whole_batch(tmp_board: Path):
    # Review 2026-07-11: repeated ref within the list = malformed input →
    # entire batch refused, board untouched (even the clean X9 item).
    ops = FileBoardOps()
    before = tmp_board.read_text(encoding="utf-8")
    result = ops.place_components_bulk(tmp_board, [
        {"reference": "X1", "footprint": "A:B", "x": 1.0, "y": 1.0},
        {"reference": "X9", "footprint": "A:B", "x": 2.0, "y": 2.0},
        {"reference": "X1", "footprint": "A:B", "x": 3.0, "y": 3.0},
    ])
    assert result["status"] == "refused"
    assert "X1" in result["reason"]
    assert result["placed"] == []
    assert tmp_board.read_text(encoding="utf-8") == before


def test_file_bulk_onboard_collisions_are_per_item(tmp_board: Path):
    # R1 identical → idempotent skip; C1 at a new position → per-item refusal
    # with the existing state; X9 is clean → placed. One write.
    ops = FileBoardOps()
    result = ops.place_components_bulk(tmp_board, [
        {"reference": "R1", "footprint": "Device:R", "x": 100.0, "y": 100.0},
        {"reference": "C1", "footprint": "Device:C", "x": 55.0, "y": 55.0},
        {"reference": "X9", "footprint": "DoesNotExist:Stub", "x": 5.0, "y": 5.0},
    ])
    assert result["idempotent"] == ["R1"]
    assert result["placed"] == ["X9"]
    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["reference"] == "C1"
    assert failure["suggested_tool"] == "move_component"
    assert failure["existing"]["x"] == 110.0

    components = ops.get_components(tmp_board)
    refs = [c["reference"] for c in components]
    assert refs.count("R1") == 1 and refs.count("C1") == 1 and refs.count("X9") == 1
    c1 = next(c for c in components if c["reference"] == "C1")
    assert c1["position"]["x"] == pytest.approx(110.0)  # refusal didn't move it


# ---------------------------------------------------------------------------
# Bridge client (PluginBoardOps) — guard runs before any TCP write
# ---------------------------------------------------------------------------

class _RecordingBridgeOps(PluginBoardOps):
    """PluginBoardOps with _call stubbed at the TCP boundary."""

    def __init__(self, board_components):
        self.board_components = board_components
        self.calls: list[str] = []

    def _call(self, op, path, **kwargs):
        self.calls.append(op)
        if op == "get_components":
            return self.board_components
        if op == "place_component":
            return {"status": "ok", "reference": kwargs["reference"]}
        if op == "place_components_bulk":
            return {"placed": [c.get("reference") for c in kwargs["components"]],
                    "failed": []}
        raise AssertionError(f"unexpected bridge op {op!r}")


_BRIDGE_BOARD = [{
    "reference": "U1", "value": "MCU",
    "footprint": "Package_QFP:LQFP-48",
    "x": 30.0, "y": 40.0, "layer": "F.Cu", "rotation": 0.0,
}]


def test_bridge_place_idempotent_skips_tcp_write():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    result = ops.place_component(
        Path("b.kicad_pcb"), "U1", "Package_QFP:LQFP-48", 30.0, 40.0)
    assert result["idempotent"] is True
    assert ops.calls == ["get_components"]  # no place ever sent


def test_bridge_place_differing_refuses_before_tcp_write():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    with pytest.raises(DuplicateRefError):
        ops.place_component(
            Path("b.kicad_pcb"), "U1", "Package_QFP:LQFP-48", 99.0, 99.0)
    assert "place_component" not in ops.calls


def test_bridge_place_new_ref_goes_through():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    result = ops.place_component(
        Path("b.kicad_pcb"), "R7", "Device:R", 10.0, 10.0)
    assert result["status"] == "ok"
    assert ops.calls == ["get_components", "place_component"]


def test_bridge_bulk_batch_dup_never_reaches_bridge():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    result = ops.place_components_bulk(Path("b.kicad_pcb"), [
        {"reference": "R7", "footprint": "A:B", "x": 1.0, "y": 1.0},
        {"reference": "R7", "footprint": "A:B", "x": 2.0, "y": 2.0},
    ])
    assert result["status"] == "refused"
    assert ops.calls == []  # refused before ANY TCP traffic


def test_bridge_bulk_sends_only_clean_items():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    result = ops.place_components_bulk(Path("b.kicad_pcb"), [
        {"reference": "U1", "footprint": "Package_QFP:LQFP-48",
         "x": 30.0, "y": 40.0},                                  # idempotent
        {"reference": "U1X", "footprint": "A:B", "x": 1.0, "y": 1.0},  # clean
    ])
    assert result["idempotent"] == ["U1"]
    assert result["placed"] == ["U1X"]
    assert result["failed"] == []
    assert ops.calls == ["get_components", "place_components_bulk"]


def test_bridge_bulk_onboard_collision_reported_per_item():
    ops = _RecordingBridgeOps(_BRIDGE_BOARD)
    result = ops.place_components_bulk(Path("b.kicad_pcb"), [
        {"reference": "U1", "footprint": "Package_QFP:LQFP-48",
         "x": 99.0, "y": 99.0},                                  # differs
        {"reference": "R7", "footprint": "A:B", "x": 1.0, "y": 1.0},  # clean
    ])
    assert result["placed"] == ["R7"]
    assert result["failed"][0]["reference"] == "U1"
    assert result["failed"][0]["suggested_tool"] == "move_component"


# ---------------------------------------------------------------------------
# Tool layer — structured refusal (REQ-DUP-3)
# ---------------------------------------------------------------------------

class _CapturingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_place_component_tool_returns_structured_refusal(tmp_path, tmp_change_log):
    from kicad_mcp.tools import board as board_tools

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    existing = ExistingComponent(
        reference="R1", lib_id="Device:R",
        x=10.0, y=20.0, rotation=0.0, layer="F.Cu",
    )
    backend = MagicMock()
    backend.get_board_modify_ops.return_value.place_component.side_effect = (
        DuplicateRefError(existing, "move_component"))

    mcp = _CapturingMCP()
    board_tools.register_tools(mcp, backend, tmp_change_log)
    out = json.loads(mcp.tools["place_component"](
        str(board), "R1", "Device:R", 50.0, 50.0))
    assert out["status"] == "refused"
    assert out["existing"]["reference"] == "R1"
    assert out["suggested_tool"] == "move_component"


# ---------------------------------------------------------------------------
# REQ-DUP-6 — no force/opt-out flag on any placement surface
# ---------------------------------------------------------------------------

def test_no_force_flag_on_any_place_surface():
    from kicad_mcp.backends.base import BoardOps
    from kicad_mcp.backends.ipc_backend import IPCBoardOps

    surfaces = [
        BoardOps.place_component, BoardOps.place_components_bulk,
        FileBoardOps.place_component, FileBoardOps.place_components_bulk,
        PluginBoardOps.place_component, PluginBoardOps.place_components_bulk,
        IPCBoardOps.place_component,
    ]
    for fn in surfaces:
        for name in inspect.signature(fn).parameters:
            assert not any(word in name.lower()
                           for word in ("force", "allow", "override")), (
                f"{fn.__qualname__} exposes an opt-out param {name!r}")
