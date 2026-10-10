"""F-DV9 (DV build) — ``_LiveBoardOps`` must forward zone create/delete.

The IPC-primary wrapper forwarded every ``BoardOps`` method except
``add_zone`` / ``delete_zone``, so any session with the KiCad IPC API up
raised the raw base-class ``NotImplementedError`` on zone creation even
though all four concrete backends implement the ops (RW-S2 Z1/Z2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import BoardOps
from kicad_mcp_plugin.backends.plugin_direct import _LiveBoardOps


class _StubOps(BoardOps):
    """Instantiable BoardOps: abstract reads stubbed, zone ops inherited."""

    def read_board(self, path: Path) -> dict[str, Any]:
        return {}

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_board_info(self, path: Path) -> dict[str, Any]:
        return {}


class _RecordingOps(_StubOps):
    """Stub backend that records zone calls and answers like the IPC ops."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def add_zone(  # type: ignore[override]
        self, path: Path, net: str, layer: str,
        outline: list[dict[str, float]], **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("add_zone", (path, net, layer, outline), kwargs))
        return {"status": "success", "served_by": self.tag}

    def delete_zone(
        self, path: Path, name: str = "", uuid: str = "",
    ) -> dict[str, Any]:
        self.calls.append(("delete_zone", (path,), {"name": name, "uuid": uuid}))
        return {"status": "success", "served_by": self.tag}


def _wrapper(ipc_ops: BoardOps, bridge_ops: BoardOps | None) -> _LiveBoardOps:
    def bridge_supplier() -> BoardOps:
        if bridge_ops is None:
            raise AssertionError("bridge fallback must not be used here")
        return bridge_ops

    return _LiveBoardOps(ipc_ops, bridge_supplier, lambda path: None)


def test_add_zone_forwards_to_ipc_with_kwargs_intact() -> None:
    ipc = _RecordingOps("ipc")
    ops = _wrapper(ipc, None)
    outline = [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]
    result = ops.add_zone(
        Path("b.kicad_pcb"), "GND", "B.Cu", outline,
        clearance_mm=0.635, min_thickness_mm=0.381,
        pad_connection="thermal", thermal_gap_mm=0.254,
        thermal_spoke_width_mm=0.50038, name="pour", refill=True,
    )
    assert result == {"status": "success", "served_by": "ipc"}
    method, args, kwargs = ipc.calls[0]
    assert method == "add_zone"
    assert args == (Path("b.kicad_pcb"), "GND", "B.Cu", outline)
    assert kwargs == {
        "clearance_mm": 0.635, "min_thickness_mm": 0.381,
        "pad_connection": "thermal", "thermal_gap_mm": 0.254,
        "thermal_spoke_width_mm": 0.50038, "name": "pour", "refill": True,
    }


def test_delete_zone_forwards_to_ipc() -> None:
    ipc = _RecordingOps("ipc")
    ops = _wrapper(ipc, None)
    result = ops.delete_zone(Path("b.kicad_pcb"), name="pour")
    assert result == {"status": "success", "served_by": "ipc"}
    assert ipc.calls == [
        ("delete_zone", (Path("b.kicad_pcb"),), {"name": "pour", "uuid": ""}),
    ]


def test_add_zone_falls_back_to_bridge_when_ipc_lacks_it() -> None:
    bridge = _RecordingOps("bridge")
    # _StubOps inherits the base add_zone, which raises NotImplementedError
    # like a backend without zone support — _call must retry on the bridge.
    ops = _wrapper(_StubOps(), bridge)
    outline = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}]
    result = ops.add_zone(Path("b.kicad_pcb"), "GND", "B.Cu", outline)
    assert result == {"status": "success", "served_by": "bridge"}
    assert bridge.calls[0][0] == "add_zone"


def test_wrapper_covers_entire_boardops_surface() -> None:
    """Recurrence guard: a new BoardOps method must gain a wrapper forwarder.

    _LiveBoardOps inherits the base stubs, so a missing override silently
    raises NotImplementedError at the wrapper instead of routing to IPC or
    the bridge — exactly how F-DV9 shipped.
    """
    base_methods = {
        name for name, value in vars(BoardOps).items()
        if callable(value) and not name.startswith("_")
    }
    wrapper_methods = set(vars(_LiveBoardOps))
    missing = sorted(base_methods - wrapper_methods)
    assert missing == [], (
        f"_LiveBoardOps is missing forwarders for BoardOps methods: {missing}"
    )
