"""Routing / fallback / safe-refuse tests for PluginDirectBackend (F1 step 4).

Spec §4 (bridge-board-access): T-ROUTE-1/2/3 resolver matrix, T-SAFE-1/2/3/4,
T-ROUTE-4 per-op fallback, T-ROUTE-5 telemetry. All backends mocked; runs
without KiCad.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.backends.base import BoardOps
from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.backends.ipc_connection import IPCUnavailableError
from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
)
from kicad_mcp.models.errors import SafeRefuseError
from kicad_mcp_plugin.backends import plugin_direct
from kicad_mcp_plugin.backends.plugin_direct import (
    PluginDirectBackend,
    _LiveBoardOps,
    _StaleFileBoardOps,
)

BOARD = Path("D:/proj/test_board.kicad_pcb")


class FakeIPCOps(BoardOps):
    """IPC-side ops double: reads succeed, selected methods signal fallback."""

    def __init__(self):
        self.calls: list[str] = []

    def read_board(self, path):
        self.calls.append("read_board")
        return {"info": {"title": "ipc"}, "components": [], "nets": [], "tracks": []}

    def get_components(self, path):
        self.calls.append("get_components")
        return []

    def get_nets(self, path):
        return []

    def get_tracks(self, path):
        return []

    def get_board_info(self, path):
        return {"title": "ipc"}

    def move_component(self, path, reference, x, y, rotation=None):
        self.calls.append("move_component")
        return {"status": "ok", "reference": reference, "x": x, "y": y,
                "rotation": rotation}

    def place_component(self, path, reference, footprint, x, y,
                        layer="F.Cu", rotation=0.0):
        # mirrors the live-verified KiCad 9.0.7 behavior
        raise NotImplementedError("IPC create_items did not materialize footprint")

    def assign_net(self, path, reference, pad, net):
        raise IPCUnavailableError("connection dropped mid-op")


class FakeBridgeOps:
    """Bridge-side double recording what fell through to it."""

    def __init__(self):
        self.calls: list[str] = []

    def place_component(self, path, reference, footprint, x, y,
                        layer="F.Cu", rotation=0.0):
        self.calls.append("place_component")
        return {"status": "ok", "served_by": "bridge"}

    def assign_net(self, path, reference, pad, net):
        self.calls.append("assign_net")
        return {"status": "ok", "served_by": "bridge"}

    def set_footprint_value(self, path, reference, value):
        self.calls.append("set_footprint_value")
        return {"status": "ok", "served_by": "bridge"}

    def auto_place(self, path, board_x, board_y, board_width, board_height,
                   clearance_mm=1.5, anchors=None, strategy="net_aware"):
        self.calls.append("auto_place")
        return {"status": "ok", "served_by": "bridge"}


class FakeIPC:
    """IPCBackend double with a switchable availability flag."""

    def __init__(self, available: bool, ops: FakeIPCOps | None = None):
        self.available = available
        self.ops = ops if ops is not None else FakeIPCOps()
        self.active_project: dict[str, Any] = {
            "board_path": "D:/proj/test_board.kicad_pcb",
            "project_name": "test_board", "project_path": "D:/proj",
        }
        self.active_project_error: Exception | None = None

    def is_available(self) -> bool:
        return self.available

    def get_board_ops(self):
        return self.ops

    def get_active_project(self):
        if self.active_project_error is not None:
            raise self.active_project_error
        return self.active_project


@pytest.fixture
def router(monkeypatch):
    """PluginDirectBackend with every external dependency controllable."""
    def no_bridge(method, timeout, **kwargs):
        raise BridgeTemporarilyUnavailableError("no bridge in tests")
    monkeypatch.setattr(plugin_direct, "_tcp_call", no_bridge)
    backend = PluginDirectBackend()

    def set_state(*, ipc: bool = False, bridge: bool = False, kicad: bool = False,
                  ipc_ops: FakeIPCOps | None = None):
        backend._ipc = FakeIPC(ipc, ops=ipc_ops)
        backend._bridge_available = bridge
        monkeypatch.setattr(backend, "_probe_bridge", lambda: bridge)
        monkeypatch.setattr(plugin_direct, "is_kicad_running", lambda: kicad)
        return backend

    return set_state


# ---------------------------------------------------------------------------
# T-ROUTE-1/2/3 — resolver matrix
# ---------------------------------------------------------------------------

class TestResolverMatrix:
    def test_ipc_up_serves_ipc_first(self, router):
        backend = router(ipc=True, bridge=True, kicad=True)
        ops = backend.get_board_ops()
        assert isinstance(ops, _LiveBoardOps)
        assert backend._live_path["BOARD_READ"] == "ipc"

    def test_ipc_down_bridge_up_serves_bridge(self, router):
        backend = router(ipc=False, bridge=True, kicad=True)
        ops = backend.get_board_modify_ops()
        assert isinstance(ops, PluginBoardOps)
        assert backend._live_path["BOARD_MODIFY"] == "bridge"

    @pytest.mark.parametrize("getter,cap,write", [
        ("get_board_ops", "BOARD_READ", False),
        ("get_board_modify_ops", "BOARD_MODIFY", True),
        ("get_zone_refill_ops", "ZONE_REFILL", True),
        ("get_board_stackup_ops", "BOARD_STACKUP", False),
    ])
    def test_all_four_getters_route(self, router, getter, cap, write):
        backend = router(ipc=True)
        ops = getattr(backend, getter)()
        assert isinstance(ops, _LiveBoardOps)
        assert backend._live_path[cap] == "ipc"

    def test_bridge_reprobe_on_resolution(self, router, monkeypatch):
        # bridge was marked down but is answering again → resolver reconnects
        backend = router(ipc=False, bridge=False, kicad=True)
        monkeypatch.setattr(backend, "_probe_bridge", lambda: True)
        ops = backend.get_board_ops()
        assert isinstance(ops, PluginBoardOps)
        assert backend._bridge_available is True


# ---------------------------------------------------------------------------
# T-SAFE-1/2/3 — degradation with both live paths down
# ---------------------------------------------------------------------------

class TestSafeDegradation:
    def test_write_kicad_open_safe_refuses(self, router):
        backend = router(ipc=False, bridge=False, kicad=True)
        with pytest.raises(SafeRefuseError) as exc_info:
            backend.get_board_modify_ops()
        err = exc_info.value
        assert err.capability == "BOARD_MODIFY"
        assert err.paths_tried == ["ipc", "bridge"]
        assert err.remedy  # actionable text (REQ-LIFE-2 wording)
        assert "clobbered" in str(err)

    def test_write_kicad_closed_uses_file(self, router):
        backend = router(ipc=False, bridge=False, kicad=False)
        ops = backend.get_board_modify_ops()
        assert isinstance(ops, FileBoardOps)
        assert backend._live_path["BOARD_MODIFY"] == "file"

    def test_read_kicad_open_degrades_stale_flagged(self, router):
        backend = router(ipc=False, bridge=False, kicad=True)
        ops = backend.get_board_ops()
        assert isinstance(ops, _StaleFileBoardOps)
        assert backend._live_path["BOARD_READ"] == "file:stale"

    def test_read_kicad_closed_uses_plain_file(self, router):
        # disk is authoritative with no live session — no stale flag
        backend = router(ipc=False, bridge=False, kicad=False)
        ops = backend.get_board_ops()
        assert isinstance(ops, FileBoardOps)
        assert backend._live_path["BOARD_READ"] == "file"


class TestStaleFileOps:
    class _StubFileOps(BoardOps):
        def read_board(self, path):
            return {"info": {"title": "disk"}}

        def get_components(self, path):
            return [{"reference": "R1"}]

        def get_nets(self, path):
            return []

        def get_tracks(self, path):
            return []

        def get_board_info(self, path):
            return {"title": "disk"}

        def get_design_rules(self, path):
            return {"clearance": 0.2}

    def test_dict_reads_carry_stale_flag(self):
        ops = _StaleFileBoardOps(self._StubFileOps())
        assert ops.read_board(BOARD)["stale"] is True
        assert ops.get_board_info(BOARD)["stale"] is True
        assert ops.get_design_rules(BOARD)["stale"] is True

    def test_list_reads_pass_through(self):
        ops = _StaleFileBoardOps(self._StubFileOps())
        assert ops.get_components(BOARD) == [{"reference": "R1"}]

    def test_writes_keep_base_refusal(self):
        ops = _StaleFileBoardOps(self._StubFileOps())
        with pytest.raises(NotImplementedError):
            ops.move_component(BOARD, "R1", 1.0, 1.0)


# ---------------------------------------------------------------------------
# T-ROUTE-4 — per-op fallback inside _LiveBoardOps
# ---------------------------------------------------------------------------

class TestPerOpFallback:
    def _proxy(self, ipc_ops=None, bridge_ops=None, bridge_down=False):
        ipc_ops = ipc_ops if ipc_ops is not None else FakeIPCOps()
        bridge_ops = bridge_ops if bridge_ops is not None else FakeBridgeOps()
        self.paths: list[str] = []

        def supplier():
            if bridge_down:
                raise BridgeTemporarilyUnavailableError("bridge down")
            return bridge_ops
        return (_LiveBoardOps(ipc_ops, supplier, self.paths.append),
                ipc_ops, bridge_ops)

    def test_ipc_covered_op_stays_on_ipc(self):
        proxy, ipc_ops, bridge_ops = self._proxy()
        result = proxy.move_component(BOARD, "R1", 5.0, 6.0)
        assert result["status"] == "ok"
        assert ipc_ops.calls == ["move_component"]
        assert bridge_ops.calls == []
        assert self.paths == ["ipc"]

    def test_notimplemented_falls_to_bridge(self):
        # the live-verified place_component case
        proxy, ipc_ops, bridge_ops = self._proxy()
        result = proxy.place_component(BOARD, "R1", "Lib:FP", 1.0, 2.0)
        assert result["served_by"] == "bridge"
        assert bridge_ops.calls == ["place_component"]
        assert self.paths == ["bridge"]

    def test_ipc_drop_mid_op_falls_to_bridge(self):
        proxy, ipc_ops, bridge_ops = self._proxy()
        result = proxy.assign_net(BOARD, "U1", "1", "GND")
        assert result["served_by"] == "bridge"
        assert bridge_ops.calls == ["assign_net"]

    def test_bridge_extra_method_falls_to_bridge(self):
        # set_footprint_value is not on the BoardOps base → AttributeError path
        proxy, ipc_ops, bridge_ops = self._proxy()
        result = proxy.set_footprint_value(BOARD, "R1", "10k")
        assert result["served_by"] == "bridge"
        assert bridge_ops.calls == ["set_footprint_value"]

    def test_s2_row_default_falls_to_bridge(self):
        # auto_place keeps the BoardOps base default on the IPC side (S2 row)
        proxy, ipc_ops, bridge_ops = self._proxy()
        result = proxy.auto_place(BOARD, 0.0, 0.0, 100.0, 80.0)
        assert result["served_by"] == "bridge"
        assert bridge_ops.calls == ["auto_place"]

    def test_fallback_with_bridge_down_propagates(self):
        proxy, ipc_ops, bridge_ops = self._proxy(bridge_down=True)
        with pytest.raises(BridgeTemporarilyUnavailableError):
            proxy.place_component(BOARD, "R1", "Lib:FP", 1.0, 2.0)


# ---------------------------------------------------------------------------
# T-ROUTE-5 — telemetry + get_active_project routing
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_live_path_last_in_status(self, router):
        backend = router(ipc=True)
        backend.get_board_ops()
        status = backend.get_status()
        assert status["live_path_last"]["BOARD_READ"] == "ipc"

    def test_capability_routing_values_stay_strings(self, router):
        # the health monitor groups by these values — never nest dicts here
        backend = router(ipc=False)
        status = backend.get_status()
        assert all(isinstance(v, str)
                   for v in status["capability_routing"].values())

    def test_primary_backend_reflects_ipc(self, router):
        backend = router(ipc=True)
        assert backend.get_status()["primary_backend"] == "ipc"
        backend = router(ipc=False)
        assert backend.get_status()["primary_backend"] == "plugin"

    def test_ipc_listed_in_active_backends(self, router):
        backend = router(ipc=True)
        entries = {b["name"]: b for b in backend.get_status()["active_backends"]}
        assert entries["ipc"]["available"] is True
        assert "BOARD_READ" in entries["ipc"]["capabilities"]
        assert entries["plugin"]["available"] is True  # bridge stays wired (NG3)


class TestGetActiveProject:
    def test_ipc_first(self, router):
        backend = router(ipc=True, bridge=True)
        result = backend.get_active_project()
        assert result["project_name"] == "test_board"
        assert backend._live_path["ACTIVE_PROJECT"] == "ipc"

    def test_falls_to_bridge_when_ipc_down(self, router, monkeypatch):
        backend = router(ipc=False, bridge=True)
        monkeypatch.setattr(
            plugin_direct, "_tcp_call",
            lambda method, timeout, **kw: {"board_path": "from_bridge"})
        result = backend.get_active_project()
        assert result["board_path"] == "from_bridge"
        assert backend._live_path["ACTIVE_PROJECT"] == "bridge"

    def test_ipc_drop_mid_call_falls_to_bridge(self, router, monkeypatch):
        backend = router(ipc=True, bridge=True)
        backend._ipc.active_project_error = IPCUnavailableError("dropped")
        monkeypatch.setattr(
            plugin_direct, "_tcp_call",
            lambda method, timeout, **kw: {"board_path": "from_bridge"})
        result = backend.get_active_project()
        assert result["board_path"] == "from_bridge"
