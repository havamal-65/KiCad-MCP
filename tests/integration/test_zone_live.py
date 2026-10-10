"""spec-zones live integration — zone round-trip through the bridge.

Requires KICAD_INTEGRATION=1, pcbnew open on tests/_scratch/test_board.kicad_pcb
with bridge >= 2.7.0-zones.

Adds a GND zone to the LIVE scratch board through the bridge (built with real
pcbnew ZONE objects, filled by the real ZONE_FILLER), confirms it lands on disk
in a form the Z2 parser reads back, refills it, then deletes it through the
bridge so the scratch board returns to its original state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import parse_board_zones
from kicad_mcp.backends.plugin_backend import PluginBoardOps

_SCRATCH_BOARD = Path(__file__).parent.parent / "_scratch" / "test_board.kicad_pcb"

_OUTLINE = [
    {"x": 2.0, "y": 2.0},
    {"x": 30.0, "y": 2.0},
    {"x": 30.0, "y": 20.0},
    {"x": 2.0, "y": 20.0},
]


@pytest.fixture
def ops() -> PluginBoardOps:
    return PluginBoardOps()


def _zone_by_name(path: Path, name: str) -> dict | None:
    zones = parse_board_zones(path.read_text(encoding="utf-8"))
    return next((z for z in zones if z["name"] == name), None)


def _pick_net(path: Path) -> str:
    """A real net on the scratch board (prefer a ground-ish one), so the test
    works on whatever fixture board the session has open."""
    content = path.read_text(encoding="utf-8")
    nets = [n for n in re.findall(r'\(net (?:\d+ )?"([^"]+)"\)', content)
            if n and not n.startswith("unconnected-")]
    assert nets, "scratch board has no named nets"
    return next((n for n in nets if "GND" in n.upper()), nets[0])


def test_zone_live_round_trip(bridge_session, ops: PluginBoardOps) -> None:
    assert _SCRATCH_BOARD.exists(), "scratch board missing — see integration README"
    before = parse_board_zones(_SCRATCH_BOARD.read_text(encoding="utf-8"))
    assert _zone_by_name(_SCRATCH_BOARD, "it_zone") is None, (
        "leftover it_zone from a previous run — delete it first"
    )

    net = _pick_net(_SCRATCH_BOARD)
    try:
        # Z1 through the bridge: real ZONE + real ZONE_FILLER.
        result = ops.add_zone(
            _SCRATCH_BOARD, net, "B.Cu", _OUTLINE,
            name="it_zone", refill=True,
        )
        assert result["status"] in ("success", "ok"), result
        assert result["via"] == "bridge"
        assert result["filled"] is True
        assert result.get("filled_area_mm2", 0) > 0

        # The bridge save must land the zone on disk in Z2-parseable form.
        on_disk = _zone_by_name(_SCRATCH_BOARD, "it_zone")
        assert on_disk is not None
        assert on_disk["net"] == net
        assert on_disk["layers"] == ["B.Cu"]
        assert on_disk["filled"] is True
        assert on_disk["is_rule_area"] is False

        # Z5: refill on a zoned board reports >= 1 filled.
        refill = ops.refill_zones(_SCRATCH_BOARD)
        assert refill["status"] in ("success", "ok"), refill
        assert refill["zones_filled"] >= 1
    finally:
        # Z3: delete through the bridge restores the original state.
        deleted = ops.delete_zone(_SCRATCH_BOARD, name="it_zone")
        assert deleted["status"] in ("success", "ok"), deleted

    after = parse_board_zones(_SCRATCH_BOARD.read_text(encoding="utf-8"))
    assert _zone_by_name(_SCRATCH_BOARD, "it_zone") is None
    assert len(after) == len(before)


def test_zone_live_error_paths(bridge_session, ops: PluginBoardOps) -> None:
    bad_net = ops.add_zone(
        _SCRATCH_BOARD, "NO_SUCH_NET", "B.Cu", _OUTLINE, refill=False,
    )
    assert bad_net["status"] == "error"
    assert bad_net["error_code"] == "net_not_found"

    bad_layer = ops.add_zone(
        _SCRATCH_BOARD, _pick_net(_SCRATCH_BOARD), "Nope.Cu", _OUTLINE,
        refill=False,
    )
    assert bad_layer["status"] == "error"
    assert bad_layer["error_code"] == "invalid_layer"

    missing = ops.delete_zone(_SCRATCH_BOARD, name="no_such_zone")
    assert missing["status"] == "error"
    assert missing["error_code"] == "zone_not_found"
