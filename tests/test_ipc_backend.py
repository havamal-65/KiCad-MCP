"""Unit tests for the IPC board backend (F1 / S1) — kipy fully mocked.

Covers `ipc_connection.py` (spec §2.1 / build-order step 1):
- T-IPC-8 (REQ-IPC-8) — missing kipy degrades cleanly, never raises on construct
- REQ-ROUTE-3 — "available" requires server reachable AND a loaded board open
- REQ-LIFE-2 — refusal carries actionable remedy text (pref-off / restart heuristic)
- REQ-LIFE-3 — failed probe drops the handle; next call reconnects
- REQ-GATE-1 — board_ready distinguishes loaded vs still-loading

And `ipc_backend.py` (spec §2.2–§2.3 / step 2 — reads + _commit):
- T-IPC-1 (REQ-IPC-1) — IPCBoardOps is a real BoardOps drop-in
- T-IPC-7 (REQ-IPC-7) — capability set exact; is_available never static
- T-IPC-6 (REQ-IPC-6) — mid-commit error drops the commit, never pushes
- read shapes match the bridge handlers byte-for-byte (REQ-COV-2)

Runs without KiCad installed (the kipy *library* is a declared dep and its
pure helpers — units, layer names — are exercised for real).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from kicad_mcp.backends import ipc_connection
from kicad_mcp.backends.ipc_backend import IPCBackend, IPCBoardOps
from kicad_mcp.backends.ipc_connection import (
    IPCConnection,
    IPCUnavailableError,
    connection_remedy,
    ipc_enabled,
    read_api_server_pref,
)
from kicad_mcp.backends.base import BoardOps, BackendCapability


class FakeConnectionError(Exception):
    pass


class FakeApiError(Exception):
    pass


@pytest.fixture
def fake_kipy(monkeypatch):
    """Install a fake kipy module into ipc_connection; returns the FakeKiCad class.

    Behavior is steered through class attributes:
      ping_error / board_error — exception instance to raise, or None
      board_name — filename the fake Board reports
    """

    class FakeBoard:
        def __init__(self, name: str):
            self.name = name

    class FakeKiCad:
        ping_error: Exception | None = None
        board_error: Exception | None = None
        board_name: str = "aqs_v2.kicad_pcb"
        instances: list = []

        def __init__(self, socket_path=None, client_name=None, timeout_ms=2000):
            self.socket_path = socket_path
            self.client_name = client_name
            self.timeout_ms = timeout_ms
            type(self).instances.append(self)

        def ping(self):
            if type(self).ping_error is not None:
                raise type(self).ping_error

        def get_board(self):
            if type(self).board_error is not None:
                raise type(self).board_error
            return FakeBoard(type(self).board_name)

    fake = types.SimpleNamespace(
        KiCad=FakeKiCad,
        errors=types.SimpleNamespace(
            ConnectionError=FakeConnectionError,
            ApiError=FakeApiError,
        ),
    )
    monkeypatch.setattr(ipc_connection, "kipy", fake)
    monkeypatch.delenv("KICAD_MCP_IPC_ENABLED", raising=False)
    return FakeKiCad


# ---------------------------------------------------------------------------
# T-IPC-8 — kipy missing (REQ-IPC-8, REQ-LIFE-4)
# ---------------------------------------------------------------------------

class TestKipyMissing:
    def test_construct_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "kipy", None)
        conn = IPCConnection()  # must not raise
        assert conn.connected is False

    def test_is_available_false(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "kipy", None)
        assert IPCConnection().is_available() is False

    def test_connect_raises_structured_error_with_remedy(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "kipy", None)
        with pytest.raises(IPCUnavailableError) as exc_info:
            IPCConnection().connect()
        assert "kicad-python" in (exc_info.value.remedy or "")

    def test_probes_never_raise(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "kipy", None)
        conn = IPCConnection()
        assert conn.ping() is False
        assert conn.board_ready() is False


# ---------------------------------------------------------------------------
# Env kill-switch (KICAD_MCP_IPC_ENABLED)
# ---------------------------------------------------------------------------

class TestKillSwitch:
    @pytest.mark.parametrize("value", ["0", "false", "no", "off", " FALSE "])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", value)
        assert ipc_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", value)
        assert ipc_enabled() is True

    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_IPC_ENABLED", raising=False)
        assert ipc_enabled() is True

    def test_disabled_forces_unavailable_even_with_live_server(self, fake_kipy, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", "0")
        conn = IPCConnection()
        assert conn.is_available() is False
        with pytest.raises(IPCUnavailableError) as exc_info:
            conn.connect()
        assert "KICAD_MCP_IPC_ENABLED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Availability = reachable AND loaded board (REQ-ROUTE-3)
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_available_with_server_and_board(self, fake_kipy):
        assert IPCConnection().is_available() is True

    def test_server_up_but_no_board_is_not_available(self, fake_kipy):
        fake_kipy.board_error = FakeApiError("no board open")
        assert IPCConnection().is_available() is False

    def test_server_down_is_not_available(self, fake_kipy):
        fake_kipy.ping_error = FakeConnectionError("connection refused")
        assert IPCConnection().is_available() is False

    def test_still_loading_board_is_not_available(self, fake_kipy):
        # REQ-GATE-3: server up, document open, but no filename yet — the
        # router must advance/refuse, never touch a half-loaded board
        fake_kipy.board_name = ""
        assert IPCConnection().is_available() is False

    def test_no_board_raises_with_open_board_remedy(self, fake_kipy):
        fake_kipy.board_error = FakeApiError("no board open")
        conn = IPCConnection()
        with pytest.raises(IPCUnavailableError) as exc_info:
            conn.board()
        assert "no PCB document" in str(exc_info.value)
        assert "PCB editor" in (exc_info.value.remedy or "")
        # server itself is fine — the handle is kept (no reconnect churn)
        assert conn.connected is True

    def test_connection_drop_during_get_board_drops_handle(self, fake_kipy):
        conn = IPCConnection()
        conn.connect()
        fake_kipy.board_error = FakeConnectionError("socket died")
        with pytest.raises(IPCUnavailableError):
            conn.board()
        assert conn.connected is False


# ---------------------------------------------------------------------------
# Health probe + reconnect (REQ-LIFE-3, C1b)
# ---------------------------------------------------------------------------

class TestPingAndReconnect:
    def test_ping_ok(self, fake_kipy):
        conn = IPCConnection()
        assert conn.ping() is True
        assert conn.connected is True

    def test_failed_ping_drops_handle_then_next_call_reconnects(self, fake_kipy):
        conn = IPCConnection()
        conn.connect()
        fake_kipy.ping_error = FakeConnectionError("connection refused")
        assert conn.ping() is False
        assert conn.connected is False  # stale handle dropped
        fake_kipy.ping_error = None  # KiCad came back
        assert conn.ping() is True  # transparent reconnect on next call
        assert conn.connected is True

    def test_reconnect_dials_fresh(self, fake_kipy):
        conn = IPCConnection()
        conn.connect()
        n_before = len(fake_kipy.instances)
        conn.reconnect()
        assert len(fake_kipy.instances) == n_before + 1  # full drop + fresh dial
        assert conn.connected is True

    def test_reconnect_propagates_refusal(self, fake_kipy):
        conn = IPCConnection()
        conn.connect()
        fake_kipy.ping_error = FakeConnectionError("connection refused")
        with pytest.raises(IPCUnavailableError):
            conn.reconnect()
        assert conn.connected is False

    def test_connect_failure_carries_remedy(self, fake_kipy, monkeypatch):
        fake_kipy.ping_error = FakeConnectionError("connection refused")
        monkeypatch.setattr(ipc_connection, "read_api_server_pref", lambda: None)
        with pytest.raises(IPCUnavailableError) as exc_info:
            IPCConnection().connect()
        assert exc_info.value.remedy  # non-empty remedy text (REQ-LIFE-2)


# ---------------------------------------------------------------------------
# Board-ready gate (REQ-GATE-1)
# ---------------------------------------------------------------------------

class TestBoardReady:
    def test_loaded_board_is_ready(self, fake_kipy):
        assert IPCConnection().board_ready() is True

    def test_board_without_filename_not_ready(self, fake_kipy):
        fake_kipy.board_name = ""  # document exists but not loaded yet
        assert IPCConnection().board_ready() is False

    def test_no_board_not_ready(self, fake_kipy):
        fake_kipy.board_error = FakeApiError("no board open")
        assert IPCConnection().board_ready() is False

    def test_board_returns_fresh_handle_each_call(self, fake_kipy):
        conn = IPCConnection()
        first = conn.board()
        fake_kipy.board_name = "other.kicad_pcb"  # user switched boards
        second = conn.board()
        assert first.name == "aqs_v2.kicad_pcb"
        assert second.name == "other.kicad_pcb"  # no stale cached document


# ---------------------------------------------------------------------------
# Remedy heuristics (REQ-LIFE-2 + the 2026-07-07 restart lesson)
# ---------------------------------------------------------------------------

class TestConnectionRemedy:
    def test_pref_off_instructs_enabling(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "read_api_server_pref", lambda: False)
        remedy = connection_remedy()
        assert "Preferences" in remedy
        assert "enable" in remedy.lower()

    def test_pref_on_instructs_restart_not_retoggle(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "read_api_server_pref", lambda: True)
        remedy = connection_remedy()
        assert "Restart KiCad" in remedy
        assert "re-toggle" in remedy
        assert ".lck" in remedy  # stale-lock hint from the live 2026-07-07 case

    def test_unknown_pref_gives_generic_guidance(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "read_api_server_pref", lambda: None)
        remedy = connection_remedy()
        assert "IPC API server" in remedy


# ---------------------------------------------------------------------------
# kicad_common.json pref reading
# ---------------------------------------------------------------------------

class TestReadApiServerPref:
    def _write_common(self, root, version: str, payload: dict) -> None:
        d = root / version
        d.mkdir(parents=True)
        (d / "kicad_common.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_reads_enable_server_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: tmp_path)
        self._write_common(tmp_path, "9.0", {"api": {"enable_server": True}})
        assert read_api_server_pref() is True

    def test_reads_enable_server_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: tmp_path)
        self._write_common(tmp_path, "9.0", {"api": {"enable_server": False}})
        assert read_api_server_pref() is False

    def test_newest_version_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: tmp_path)
        self._write_common(tmp_path, "8.0", {"api": {"enable_server": False}})
        self._write_common(tmp_path, "9.0", {"api": {"enable_server": True}})
        assert read_api_server_pref() is True

    def test_missing_config_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: tmp_path)
        assert read_api_server_pref() is None

    def test_no_config_root_returns_none(self, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: None)
        assert read_api_server_pref() is None

    def test_malformed_json_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ipc_connection, "_kicad_config_root", lambda: tmp_path)
        d = tmp_path / "9.0"
        d.mkdir()
        (d / "kicad_common.json").write_text("{not json", encoding="utf-8")
        assert read_api_server_pref() is None


# ---------------------------------------------------------------------------
# Connection parameters
# ---------------------------------------------------------------------------

# ===========================================================================
# ipc_backend.py — step 2: reads + _commit
# ===========================================================================

def _nm(mm: float) -> int:
    from kipy.util.units import from_mm
    return from_mm(mm)


def _edge_layer():
    from kipy.proto.board.board_types_pb2 import BoardLayer
    return BoardLayer.BL_Edge_Cuts


def _f_cu():
    from kipy.proto.board.board_types_pb2 import BoardLayer
    return BoardLayer.BL_F_Cu


def _xy(x_mm: float, y_mm: float):
    return types.SimpleNamespace(x=_nm(x_mm), y=_nm(y_mm))


def _text_field(value: str):
    return types.SimpleNamespace(text=types.SimpleNamespace(value=value))


class FakeLibId:
    def __str__(self) -> str:
        return "Resistor_SMD:R_0805_2012Metric"


def _fake_footprint(reference="R1", value="10k", x_mm=25.0, y_mm=30.0, rotation=90.0):
    return types.SimpleNamespace(
        reference_field=_text_field(reference),
        value_field=_text_field(value),
        definition=types.SimpleNamespace(id=FakeLibId()),
        position=_xy(x_mm, y_mm),
        layer=_f_cu(),
        orientation=types.SimpleNamespace(degrees=rotation),
    )


class FakeLiveBoard:
    """Duck-typed kipy Board: real BoardLayer enums + nm units, no connection."""

    def __init__(self):
        self.name = "test_board.kicad_pcb"
        self.footprints = [_fake_footprint()]
        self.nets = [types.SimpleNamespace(name="GND", code=1)]
        self.tracks = [types.SimpleNamespace(
            start=_xy(1.0, 2.0), end=_xy(3.0, 2.0), width=_nm(0.25),
            layer=_f_cu(), net=types.SimpleNamespace(name="GND"),
        )]
        self.vias = [types.SimpleNamespace(
            position=_xy(5.0, 6.0), diameter=_nm(0.8), drill_diameter=_nm(0.4),
            net=types.SimpleNamespace(name="GND"),
        )]
        # Two Edge.Cuts shapes whose union spans 50 x 30 mm from (10, 20)
        self.shapes = [
            types.SimpleNamespace(layer=_edge_layer()),
            types.SimpleNamespace(layer=_edge_layer()),
            types.SimpleNamespace(layer=_f_cu()),  # non-edge: must be ignored
        ]
        self._bboxes = [
            types.SimpleNamespace(pos=_xy(10.0, 20.0), size=_xy(50.0, 0.1)),
            types.SimpleNamespace(pos=_xy(10.0, 20.0), size=_xy(0.1, 30.0)),
        ]
        self.title_block = types.SimpleNamespace(title="Test Board", revision="B")
        self.project = types.SimpleNamespace(name="test_board", path="D:/proj")
        # S2 rows 13/16/18 surface
        self.zones: list = []
        self.stackup = None
        self.refill_calls = 0
        # commit bookkeeping
        self.commits_begun: list = []
        self.commits_pushed: list = []
        self.commits_dropped: list = []
        self.drop_commit_error: Exception | None = None
        # write bookkeeping + server-behavior switches
        self.created: list = []
        self.updated: list = []
        self.removed: list = []
        self.save_count = 0
        self.resolve_footprint_definitions = True   # server materializes lib ids
        self.create_returns_empty = False           # server refuses creation
        self.update_returns_blank_footprint = False  # server ignores pad nets

    # reads
    def get_footprints(self):
        return self.footprints

    def get_nets(self):
        return self.nets

    def get_tracks(self):
        return self.tracks

    def get_vias(self):
        return self.vias

    def get_shapes(self):
        return self.shapes

    def get_zones(self):
        return self.zones

    def refill_zones(self):
        self.refill_calls += 1

    def get_stackup(self):
        return self.stackup

    def get_item_bounding_box(self, items):
        assert all(s.layer == _edge_layer() for s in items)  # only edges queried
        return list(self._bboxes[: len(items)])

    def get_title_block_info(self):
        return self.title_block

    def get_copper_layer_count(self):
        return 2

    def get_project(self):
        return self.project

    # commit API
    def begin_commit(self):
        commit = object()
        self.commits_begun.append(commit)
        return commit

    def push_commit(self, commit, message=""):
        self.commits_pushed.append(commit)

    def drop_commit(self, commit):
        self.commits_dropped.append(commit)
        if self.drop_commit_error is not None:
            raise self.drop_commit_error

    # write API
    def create_items(self, item):
        from kipy.board_types import FootprintInstance, Pad
        self.created.append(item)
        if self.create_returns_empty:
            return []
        if isinstance(item, FootprintInstance) and self.resolve_footprint_definitions:
            pad = Pad()
            pad.number = "1"
            item.definition.add_item(pad)  # simulate lib-id resolution
        return [item]

    def update_items(self, items):
        from kipy.board_types import FootprintInstance
        self.updated.append(items)
        if self.update_returns_blank_footprint:
            return [FootprintInstance()]
        return list(items) if isinstance(items, (list, tuple)) else [items]

    def remove_items(self, items):
        self.removed.append(list(items) if isinstance(items, (list, tuple)) else [items])

    def save(self):
        self.save_count += 1


class FakeIPCBoardConnection:
    """Duck-typed IPCConnection serving a FakeLiveBoard."""

    def __init__(self, board: FakeLiveBoard | None = None):
        self._board = board if board is not None else FakeLiveBoard()
        self.available = True

    def board(self):
        if self._board is None:
            raise IPCUnavailableError("no board open")
        return self._board

    def is_available(self) -> bool:
        return self.available


BOARD_PATH = Path("D:/proj/test_board.kicad_pcb")


@pytest.fixture
def live_board():
    return FakeLiveBoard()


@pytest.fixture
def board_ops(live_board):
    return IPCBoardOps(FakeIPCBoardConnection(live_board))


class TestIPCBoardOpsInterface:
    """T-IPC-1 (REQ-IPC-1) — a real BoardOps drop-in."""

    def test_is_board_ops(self, board_ops):
        assert isinstance(board_ops, BoardOps)

    def test_core_reads_present(self, board_ops):
        for method in ("read_board", "get_components", "get_nets",
                       "get_tracks", "get_board_info"):
            assert callable(getattr(board_ops, method))

    def test_uncovered_write_falls_to_base_notimplemented(self, board_ops):
        # REQ-ROUTE-4: methods IPC does not cover keep the base default so
        # the router falls through to the bridge — never a stubbed result.
        # (reload_board stays uncovered: KiCad 9 has no in-place reload.)
        with pytest.raises(NotImplementedError):
            board_ops.reload_board(BOARD_PATH)


class TestIPCBackendSurface:
    """T-IPC-7 (REQ-IPC-7)."""

    def test_capabilities_exact(self):
        backend = IPCBackend(FakeIPCBoardConnection())
        assert backend.capabilities == {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.ZONE_REFILL,
            BackendCapability.BOARD_STACKUP,
        }

    def test_name(self):
        assert IPCBackend(FakeIPCBoardConnection()).name == "ipc"

    def test_is_available_tracks_connection_not_static(self):
        conn = FakeIPCBoardConnection()
        backend = IPCBackend(conn)
        conn.available = True
        assert backend.is_available() is True
        conn.available = False
        assert backend.is_available() is False

    def test_get_board_ops_returns_ipc_ops(self):
        assert isinstance(IPCBackend(FakeIPCBoardConnection()).get_board_ops(),
                          IPCBoardOps)


class TestReads:
    """Read shapes must match the bridge handlers (REQ-COV-2, spec §3 rows 1–4)."""

    def test_get_board_info_shape(self, board_ops):
        info = board_ops.get_board_info(BOARD_PATH)
        assert info == {
            "title": "Test Board",
            "revision": "B",
            "layer_count": 2,
            "width_mm": 50.0,
            "height_mm": 30.0,
            "net_count": 1,
            "footprint_count": 1,
        }

    def test_get_board_info_no_outline_zero_size(self, board_ops, live_board):
        live_board.shapes = []
        info = board_ops.get_board_info(BOARD_PATH)
        assert info["width_mm"] == 0.0
        assert info["height_mm"] == 0.0

    def test_get_components_shape(self, board_ops):
        components = board_ops.get_components(BOARD_PATH)
        assert components == [{
            "reference": "R1",
            "value": "10k",
            "footprint": "Resistor_SMD:R_0805_2012Metric",
            "x": 25.0,
            "y": 30.0,
            "layer": "F.Cu",
            "rotation": 90.0,
        }]

    def test_get_nets_shape(self, board_ops):
        assert board_ops.get_nets(BOARD_PATH) == [{"net_id": 1, "name": "GND"}]

    def test_get_tracks_shape_includes_tracks_and_vias(self, board_ops):
        items = board_ops.get_tracks(BOARD_PATH)
        assert items == [
            {
                "type": "track",
                "start_x": 1.0, "start_y": 2.0, "end_x": 3.0, "end_y": 2.0,
                "width": 0.25, "layer": "F.Cu", "net": "GND",
            },
            {
                "type": "via",
                "x": 5.0, "y": 6.0, "size": 0.8, "drill": 0.4, "net": "GND",
            },
        ]

    def test_read_board_composition(self, board_ops):
        result = board_ops.read_board(BOARD_PATH)
        assert set(result.keys()) == {"info", "components", "nets", "tracks"}
        assert result["info"]["title"] == "Test Board"
        assert result["components"][0]["reference"] == "R1"

    def test_path_mismatch_refused_with_canonical_phrase(self, board_ops):
        with pytest.raises(IPCUnavailableError) as exc_info:
            board_ops.get_board_info(Path("D:/other/another_board.kicad_pcb"))
        assert "does not match open board" in str(exc_info.value)
        assert "open_kicad" in (exc_info.value.remedy or "")

    def test_path_match_is_by_filename_not_directory(self, board_ops):
        # IPC only knows the bare filename; a different directory still matches.
        info = board_ops.get_board_info(Path("C:/elsewhere/test_board.kicad_pcb"))
        assert info["title"] == "Test Board"


class TestCommit:
    """T-IPC-6 (REQ-IPC-6) — transactional writes are atomic."""

    def test_success_pushes_and_returns(self, board_ops, live_board):
        result = board_ops._commit(live_board, lambda board, commit: "done")
        assert result == "done"
        assert live_board.commits_pushed == live_board.commits_begun
        assert live_board.commits_dropped == []

    def test_mutate_error_drops_never_pushes(self, board_ops, live_board):
        def failing_mutate(board, commit):
            raise ValueError("boom mid-commit")

        with pytest.raises(ValueError, match="boom mid-commit"):
            board_ops._commit(live_board, failing_mutate)
        assert live_board.commits_dropped == live_board.commits_begun
        assert live_board.commits_pushed == []

    def test_drop_failure_does_not_mask_original_error(self, board_ops, live_board):
        live_board.drop_commit_error = RuntimeError("drop also failed")

        def failing_mutate(board, commit):
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            board_ops._commit(live_board, failing_mutate)


class TestGetActiveProject:
    """Spec §3 row 3 — bridge-shape {board_path, project_name, project_path}."""

    def test_full_shape(self, live_board):
        backend = IPCBackend(FakeIPCBoardConnection(live_board))
        result = backend.get_active_project()
        assert result["project_name"] == "test_board"
        assert result["project_path"] == "D:/proj"
        assert Path(result["board_path"]) == Path("D:/proj/test_board.kicad_pcb")

    def test_project_path_pointing_at_kicad_pro_file(self, live_board):
        live_board.project = types.SimpleNamespace(
            name="test_board", path="D:/proj/test_board.kicad_pro")
        backend = IPCBackend(FakeIPCBoardConnection(live_board))
        result = backend.get_active_project()
        assert Path(result["board_path"]) == Path("D:/proj/test_board.kicad_pcb")

    def test_project_info_best_effort(self, live_board):
        def raise_api_error():
            raise RuntimeError("project query failed")
        live_board.get_project = raise_api_error
        backend = IPCBackend(FakeIPCBoardConnection(live_board))
        result = backend.get_active_project()
        assert result["board_path"] == "test_board.kicad_pcb"
        assert result["project_name"] is None
        assert result["project_path"] is None

    def test_no_board_raises_unavailable(self):
        conn = FakeIPCBoardConnection()
        conn._board = None
        backend = IPCBackend(conn)
        with pytest.raises(IPCUnavailableError):
            backend.get_active_project()


# ---------------------------------------------------------------------------
# Core writes (spec §3 rows 5–12) — T-IPC-core (REQ-IPC-4/5)
# ---------------------------------------------------------------------------

def _real_footprint(reference: str = "U1", pad_numbers: tuple = ("1", "1", "2")):
    """A real kipy FootprintInstance with real Pads (client-side only)."""
    from kipy.board_types import FootprintInstance, Pad
    fp = FootprintInstance()
    fp.reference_field.text.value = reference
    fp.layer = _f_cu()
    for number in pad_numbers:
        pad = Pad()
        pad.number = number
        fp.definition.add_item(pad)
    return fp


def _real_net(name: str = "GND"):
    from kipy.board_types import Net
    net = Net()
    net.name = name
    return net


@pytest.fixture
def write_board(live_board, monkeypatch):
    """FakeLiveBoard reconfigured with real kipy items for the write paths.

    _bridge_save is pinned to False (no bridge in unit tests) so the flush
    falls through to the IPC-side board.save() and save_count stays meaningful.
    """
    from kicad_mcp.backends import ipc_backend
    monkeypatch.setattr(ipc_backend, "_bridge_save", lambda path: False)
    live_board.footprints = [_real_footprint()]
    live_board.nets = [_real_net("GND")]
    return live_board


@pytest.fixture
def write_ops(write_board):
    return IPCBoardOps(FakeIPCBoardConnection(write_board))


class TestPlaceComponent:
    def test_success_bridge_shape(self, write_ops, write_board):
        result = write_ops.place_component(
            BOARD_PATH, "R5", "Resistor_SMD:R_0805_2012Metric", 12.0, 34.0,
            layer="B.Cu", rotation=45.0)
        assert result == {"status": "ok", "reference": "R5",
                          "footprint": "Resistor_SMD:R_0805_2012Metric",
                          "x": 12.0, "y": 34.0, "layer": "B.Cu", "rotation": 45.0}
        from kipy.board_types import FootprintInstance
        from kipy.util.board_layer import canonical_name
        from kipy.util.units import to_mm
        created = write_board.created[0]
        assert isinstance(created, FootprintInstance)
        assert str(created.definition.id) == "Resistor_SMD:R_0805_2012Metric"
        assert created.reference_field.text.value == "R5"
        assert to_mm(created.position.x) == 12.0
        assert canonical_name(created.layer) == "B.Cu"
        assert created.orientation.degrees == 45.0
        assert write_board.commits_pushed == write_board.commits_begun
        assert write_board.save_count == 1  # disk coherence after live write

    def test_unresolved_lib_id_drops_and_signals_fallback(self, write_ops, write_board):
        write_board.resolve_footprint_definitions = False
        with pytest.raises(NotImplementedError, match="bridge path"):
            write_ops.place_component(BOARD_PATH, "R5", "Nope:Missing", 1.0, 2.0)
        assert write_board.commits_dropped == write_board.commits_begun
        assert write_board.commits_pushed == []
        assert write_board.save_count == 0  # nothing landed, nothing saved


class TestMoveComponent:
    def test_moves_and_returns_bridge_shape(self, write_ops, write_board):
        from kipy.util.units import to_mm
        result = write_ops.move_component(BOARD_PATH, "U1", 50.0, 60.0, rotation=180.0)
        assert result == {"status": "ok", "reference": "U1",
                          "x": 50.0, "y": 60.0, "rotation": 180.0}
        moved = write_board.updated[0]
        assert to_mm(moved.position.x) == 50.0
        assert to_mm(moved.position.y) == 60.0
        assert moved.orientation.degrees == 180.0
        assert write_board.commits_pushed == write_board.commits_begun
        assert write_board.save_count == 1

    def test_rotation_none_keeps_and_reports_current(self, write_ops, write_board):
        from kipy.geometry import Angle
        write_board.footprints[0].orientation = Angle.from_degrees(90.0)
        result = write_ops.move_component(BOARD_PATH, "U1", 5.0, 5.0)
        assert result["rotation"] == 90.0

    def test_unknown_reference_value_error(self, write_ops):
        with pytest.raises(ValueError, match="'R99' not found on board"):
            write_ops.move_component(BOARD_PATH, "R99", 1.0, 1.0)


class TestAddTrackAndVia:
    def test_add_track_maps_units_layer_net(self, write_ops, write_board):
        from kipy.board_types import Track
        from kipy.util.units import to_mm
        result = write_ops.add_track(BOARD_PATH, 1.0, 2.0, 3.0, 2.0, 0.25,
                                     layer="B.Cu", net="GND")
        assert result == {"status": "ok", "start_x": 1.0, "start_y": 2.0,
                          "end_x": 3.0, "end_y": 2.0, "width": 0.25,
                          "layer": "B.Cu", "net": "GND"}
        track = write_board.created[0]
        assert isinstance(track, Track)
        assert to_mm(track.width) == 0.25
        assert track.net.name == "GND"
        assert write_board.save_count == 1

    def test_add_track_unknown_net_silently_skipped(self, write_ops, write_board):
        # bridge parity: FindNet miss leaves the track netless, still succeeds
        result = write_ops.add_track(BOARD_PATH, 0.0, 0.0, 1.0, 0.0, 0.2,
                                     net="NO_SUCH_NET")
        assert result["status"] == "ok"
        assert write_board.created[0].net.name == ""

    def test_add_via_types_and_sizes(self, write_ops, write_board):
        from kipy.board_types import Via
        from kipy.proto.board.board_types_pb2 import ViaType
        from kipy.util.units import to_mm
        result = write_ops.add_via(BOARD_PATH, 5.0, 6.0, size=0.9, drill=0.5,
                                   net="GND", via_type="blind")
        assert result == {"status": "ok", "x": 5.0, "y": 6.0, "size": 0.9,
                          "drill": 0.5, "net": "GND", "via_type": "blind"}
        via = write_board.created[0]
        assert isinstance(via, Via)
        assert via.type == ViaType.VT_BLIND_BURIED
        assert to_mm(via.diameter) == 0.9
        assert to_mm(via.drill_diameter) == 0.5
        assert via.net.name == "GND"

    def test_add_via_unknown_type_defaults_to_through(self, write_ops, write_board):
        from kipy.proto.board.board_types_pb2 import ViaType
        write_ops.add_via(BOARD_PATH, 1.0, 1.0, via_type="weird")
        assert write_board.created[0].type == ViaType.VT_THROUGH


class TestAssignNet:
    def test_existing_net_updates_all_matching_pads(self, write_ops, write_board):
        result = write_ops.assign_net(BOARD_PATH, "U1", "1", "GND")
        # fixture has TWO physical pads numbered "1" (thermal-array contract)
        assert result == {"status": "ok", "reference": "U1", "pad": "1",
                          "net": "GND", "pads_updated": 2}
        updated_fp = write_board.updated[0]
        assert [p.net.name for p in updated_fp.definition.pads
                if p.number == "1"] == ["GND", "GND"]
        assert write_board.commits_pushed == write_board.commits_begun
        assert write_board.save_count == 1

    def test_unknown_pad_value_error(self, write_ops):
        with pytest.raises(ValueError, match="Pad '9' not found on 'U1'"):
            write_ops.assign_net(BOARD_PATH, "U1", "9", "GND")

    def test_unknown_reference_value_error(self, write_ops):
        with pytest.raises(ValueError, match="'R99' not found on board"):
            write_ops.assign_net(BOARD_PATH, "R99", "1", "GND")

    def test_missing_net_created_on_the_fly(self, write_ops, write_board):
        from kipy.board_types import Net
        result = write_ops.assign_net(BOARD_PATH, "U1", "2", "NEW_NET")
        assert result["status"] == "ok"
        assert result["pads_updated"] == 1
        assert isinstance(write_board.created[0], Net)
        assert write_board.created[0].name == "NEW_NET"

    def test_net_creation_refused_signals_fallback(self, write_ops, write_board):
        write_board.create_returns_empty = True
        with pytest.raises(NotImplementedError, match="bridge path"):
            write_ops.assign_net(BOARD_PATH, "U1", "1", "NEW_NET")
        assert write_board.commits_dropped == write_board.commits_begun
        assert write_board.save_count == 0

    def test_server_ignoring_pad_net_signals_fallback(self, write_ops, write_board):
        write_board.update_returns_blank_footprint = True
        with pytest.raises(NotImplementedError, match="bridge path"):
            write_ops.assign_net(BOARD_PATH, "U1", "1", "GND")
        assert write_board.commits_dropped == write_board.commits_begun


class TestAddBoardOutline:
    def test_creates_rectangle_bridge_shape(self, write_ops, write_board):
        from kipy.board_types import BoardRectangle
        from kipy.util.units import to_mm
        write_board.shapes = []  # no pre-existing outline
        result = write_ops.add_board_outline(BOARD_PATH, 10.0, 20.0, 50.0, 30.0)
        assert result == {"success": True, "x": 10.0, "y": 20.0,
                          "width": 50.0, "height": 30.0, "x2": 60.0, "y2": 50.0}
        rect = write_board.created[0]
        assert isinstance(rect, BoardRectangle)
        assert rect.layer == _edge_layer()
        assert to_mm(rect.top_left.x) == 10.0
        assert to_mm(rect.bottom_right.x) == 60.0
        assert to_mm(rect.attributes.stroke.width) == 0.05
        assert write_board.removed == []  # nothing stale to replace
        assert write_board.save_count == 1

    def test_replaces_existing_edge_cuts_atomically(self, write_ops, write_board):
        # fixture ships 2 Edge.Cuts shapes + 1 F.Cu shape
        write_ops.add_board_outline(BOARD_PATH, 0.0, 0.0, 100.0, 80.0)
        assert len(write_board.removed) == 1
        assert len(write_board.removed[0]) == 2  # only the Edge.Cuts shapes
        assert write_board.commits_pushed == write_board.commits_begun


class TestSaveRouting:
    """Post-commit flush prefers the bridge so ITS #14C mtime baseline stays
    current in mixed IPC/bridge sessions (live-caught in the S1 step-7 batch:
    an IPC-side save tripped the bridge's stale-board guard)."""

    def test_bridge_save_preferred_when_bridge_up(self, write_ops, write_board,
                                                  monkeypatch):
        from kicad_mcp.backends import ipc_backend
        bridge_saves: list = []
        monkeypatch.setattr(ipc_backend, "_bridge_save",
                            lambda path: bridge_saves.append(path) or True)
        write_ops.move_component(BOARD_PATH, "U1", 1.0, 2.0)
        assert bridge_saves == [BOARD_PATH]
        assert write_board.save_count == 0  # IPC save skipped — bridge saved

    def test_ipc_save_when_bridge_down(self, write_ops, write_board):
        # write_board fixture pins _bridge_save to False
        write_ops.move_component(BOARD_PATH, "U1", 1.0, 2.0)
        assert write_board.save_count == 1

    def test_bridge_save_helper_returns_false_on_unreachable(self, monkeypatch):
        from kicad_mcp.backends import ipc_backend
        from kicad_mcp.backends import plugin_backend

        def refuse(method, timeout, **kwargs):
            raise plugin_backend.BridgeTemporarilyUnavailableError("down")
        monkeypatch.setattr(plugin_backend, "_tcp_call", refuse)
        assert ipc_backend._bridge_save(BOARD_PATH) is False

    def test_bridge_save_helper_calls_save_board(self, monkeypatch):
        from kicad_mcp.backends import ipc_backend
        from kicad_mcp.backends import plugin_backend
        calls: list = []

        def accept(method, timeout, **kwargs):
            calls.append((method, kwargs))
            return {"status": "ok"}
        monkeypatch.setattr(plugin_backend, "_tcp_call", accept)
        assert ipc_backend._bridge_save(BOARD_PATH) is True
        assert calls[0][0] == "save_board"
        assert calls[0][1]["path"] == str(BOARD_PATH)


class TestClearRoutes:
    @pytest.fixture
    def disk_board(self, tmp_path):
        board_file = tmp_path / "test_board.kicad_pcb"
        board_file.write_text("(kicad_pcb pre-clear-state)", encoding="utf-8")
        return board_file

    def test_removes_tracks_and_vias_with_backup(self, write_ops, write_board, disk_board):
        result = write_ops.clear_routes(disk_board, backup=True)
        assert result["status"] == "success"
        assert result["tracks_removed"] == 1
        assert result["vias_removed"] == 1
        backup = Path(result["backup_path"])
        assert backup.name == "test_board.clear_routes_backup.kicad_pcb"
        assert backup.read_text(encoding="utf-8") == "(kicad_pcb pre-clear-state)"
        assert len(write_board.removed[0]) == 2  # 1 track + 1 via in one call
        assert write_board.save_count == 2  # pre-backup flush + post-commit save

    def test_no_backup(self, write_ops, write_board, disk_board):
        result = write_ops.clear_routes(disk_board, backup=False)
        assert result["backup_path"] is None
        assert not list(disk_board.parent.glob("*backup*"))
        assert write_board.save_count == 1

    def test_empty_board_no_remove_call(self, write_ops, write_board, disk_board):
        write_board.tracks = []
        write_board.vias = []
        result = write_ops.clear_routes(disk_board, backup=False)
        assert result["tracks_removed"] == 0
        assert result["vias_removed"] == 0
        assert write_board.removed == []


# ---------------------------------------------------------------------------
# S2 — specialized ops (spec §3 rows 13–14, 16–21)
# ---------------------------------------------------------------------------

def _real_netclass(name: str = "Default", *, clearance=0.2, track_width=0.25,
                   via_diameter=0.8, via_drill=0.4):
    """A real kipy NetClass (fresh proto — the default arg instance is shared)."""
    from kipy.project_types import NetClass
    from kipy.proto.common.types import project_settings_pb2
    proto = project_settings_pb2.NetClass()
    proto.name = name
    if clearance is not None:
        proto.board.clearance.value_nm = _nm(clearance)
    if track_width is not None:
        proto.board.track_width.value_nm = _nm(track_width)
    if via_diameter is not None:
        layer = proto.board.via_stack.copper_layers.add()
        layer.size.x_nm = _nm(via_diameter)
    if via_drill is not None:
        proto.board.via_stack.drill.diameter.x_nm = _nm(via_drill)
    return NetClass(proto)


class TestGetDesignRules:
    """Spec §3 row 14 — Default-netclass values; gaps fall to the bridge."""

    def test_default_netclass_bridge_shape(self, board_ops, live_board):
        live_board.project = types.SimpleNamespace(
            get_net_classes=lambda: [_real_netclass("HV", clearance=0.5),
                                     _real_netclass("Default")])
        rules = board_ops.get_design_rules(BOARD_PATH)
        assert rules == {
            "clearance_mm": 0.2,
            "track_width_mm": 0.25,
            "via_diameter_mm": 0.8,
            "via_drill_mm": 0.4,
        }

    def test_partial_netclass_reports_present_fields_only(self, board_ops, live_board):
        live_board.project = types.SimpleNamespace(
            get_net_classes=lambda: [
                _real_netclass("Default", via_diameter=None, via_drill=None)])
        rules = board_ops.get_design_rules(BOARD_PATH)
        assert rules == {"clearance_mm": 0.2, "track_width_mm": 0.25}

    def test_no_default_netclass_signals_fallback(self, board_ops, live_board):
        live_board.project = types.SimpleNamespace(
            get_net_classes=lambda: [_real_netclass("HV")])
        with pytest.raises(NotImplementedError, match="bridge path"):
            board_ops.get_design_rules(BOARD_PATH)

    def test_empty_default_netclass_signals_fallback(self, board_ops, live_board):
        live_board.project = types.SimpleNamespace(
            get_net_classes=lambda: [_real_netclass(
                "Default", clearance=None, track_width=None,
                via_diameter=None, via_drill=None)])
        with pytest.raises(NotImplementedError, match="bridge path"):
            board_ops.get_design_rules(BOARD_PATH)


def _real_stackup():
    """Two-layer stackup: F.Cu copper + one FR4 dielectric (real kipy protos)."""
    from kipy.board import BoardStackup
    from kipy.proto.board import board_pb2
    proto = board_pb2.BoardStackup()
    copper = proto.layers.add()
    copper.user_name = "F.Cu"
    copper.type = board_pb2.BoardStackupLayerType.BSLT_COPPER
    copper.thickness.value_nm = _nm(0.035)
    dielectric = proto.layers.add()
    dielectric.type = board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC
    dielectric.thickness.value_nm = _nm(1.51)
    sub = dielectric.dielectric.layer.add()
    sub.epsilon_r = 4.5
    sub.loss_tangent = 0.02
    sub.material_name = "FR4"
    return BoardStackup(proto)


class TestGetStackup:
    """Spec §3 row 13 — bridge-shape {layers, source}."""

    def test_bridge_shape(self, board_ops, live_board):
        live_board.stackup = _real_stackup()
        result = board_ops.get_stackup(BOARD_PATH)
        assert result == {
            "layers": [
                {"name": "F.Cu", "type": "copper", "thickness_mm": 0.035},
                {"name": "", "type": "dielectric", "thickness_mm": 1.51,
                 "epsilon_r": 4.5, "loss_tangent": 0.02, "material": "FR4"},
            ],
            "source": "stackup_descriptor",
        }

    def test_empty_stackup_empty_layers(self, board_ops, live_board):
        from kipy.board import BoardStackup
        from kipy.proto.board import board_pb2
        live_board.stackup = BoardStackup(board_pb2.BoardStackup())
        assert board_ops.get_stackup(BOARD_PATH) == {
            "layers": [], "source": "stackup_descriptor",
        }


class TestRefillZones:
    """Spec §3 row 16 — board.refill_zones() + save, bridge shape."""

    def test_refills_and_saves(self, write_ops, write_board):
        write_board.zones = [object(), object()]
        result = write_ops.refill_zones(BOARD_PATH)
        assert result == {"status": "ok", "zones_filled": 2}
        assert write_board.refill_calls == 1
        assert write_board.save_count == 1

    def test_no_zones_skips_refill(self, write_ops, write_board):
        result = write_ops.refill_zones(BOARD_PATH)
        assert result == {"status": "ok", "zones_filled": 0}
        assert write_board.refill_calls == 0


class TestSaveBoard:
    """Spec §3 row 19 — explicit flush, bridge shape."""

    def test_saves_and_reports_path(self, write_ops, write_board):
        result = write_ops.save_board(BOARD_PATH)
        assert result == {"success": True, "path": str(BOARD_PATH)}
        assert write_board.save_count == 1


class TestAutoPlaceIPC:
    """Spec §3 row 17 — net-aware plan applied through IPC moves."""

    def test_row_strategy_signals_bridge_fallback(self, write_ops):
        with pytest.raises(NotImplementedError, match="bridge path"):
            write_ops.auto_place(BOARD_PATH, 0, 0, 100, 80, strategy="row")

    @pytest.fixture
    def engine_stub(self, monkeypatch):
        from kicad_mcp.backends import file_backend
        from kicad_mcp.utils import placement_engine as engine
        monkeypatch.setattr(file_backend, "build_engine_parts",
                            lambda path, project_dir: ["part"])
        monkeypatch.setattr(engine, "read_board_keepouts", lambda path: ([], {}))
        monkeypatch.setattr(engine, "read_diff_pair_nets", lambda path: set())
        plans = {"items": [("U1", 30.0, 40.0, 0.0)]}
        monkeypatch.setattr(
            engine, "compute_net_aware_plan",
            lambda *args, **kwargs: (plans["items"], ["w1"], 123.4))
        return plans

    def test_net_aware_plan_applied_via_ipc_moves(
            self, write_ops, write_board, engine_stub):
        result = write_ops.auto_place(BOARD_PATH, 0, 0, 100, 80)
        assert result == {
            "components_placed": 1,
            "rows": 0,
            "total_area_mm2": 123.4,
            "placements": [{"reference": "U1", "x": 30.0, "y": 40.0}],
            "warnings": ["w1"],
            "strategy": "net_aware",
        }
        from kipy.util.units import to_mm
        moved = write_board.updated[0]
        assert to_mm(moved.position.x) == 30.0
        # pre-plan flush + per-move save + final save
        assert write_board.save_count == 3

    def test_move_failure_becomes_warning(self, write_ops, engine_stub):
        engine_stub["items"] = [("R99", 1.0, 1.0, 0.0)]  # not on the board
        result = write_ops.auto_place(BOARD_PATH, 0, 0, 100, 80)
        assert result["components_placed"] == 0
        assert any("R99" in str(w) and "move failed" in str(w)
                   for w in result["warnings"])

    def test_no_parts_empty_result(self, write_ops, monkeypatch):
        from kicad_mcp.backends import file_backend
        monkeypatch.setattr(file_backend, "build_engine_parts",
                            lambda path, project_dir: [])
        result = write_ops.auto_place(BOARD_PATH, 0, 0, 100, 80)
        assert result == {
            "components_placed": 0, "rows": 0, "total_area_mm2": 0.0,
            "placements": [], "warnings": [], "strategy": "net_aware",
        }


class TestCleanBoardForRouting:
    """Spec §3 row 18 — rule-area zones + net-less tracks/vias, one commit."""

    @pytest.fixture
    def dirty_board(self, write_board):
        write_board.zones = [
            types.SimpleNamespace(is_rule_area=lambda: True),
            types.SimpleNamespace(is_rule_area=lambda: False),  # copper pour stays
        ]
        write_board.tracks.append(types.SimpleNamespace(
            start=_xy(0.0, 0.0), end=_xy(1.0, 0.0), width=_nm(0.2),
            layer=_f_cu(), net=types.SimpleNamespace(name=""),
        ))
        write_board.vias.append(types.SimpleNamespace(
            position=_xy(9.0, 9.0), diameter=_nm(0.8), drill_diameter=_nm(0.4),
            net=types.SimpleNamespace(name=""),
        ))
        return write_board

    def test_removes_keepouts_and_netless_copper(self, write_ops, dirty_board):
        result = write_ops.clean_board_for_routing(BOARD_PATH)
        assert result == {"status": "success",
                          "keepouts_removed": 1, "tracks_removed": 2}
        assert len(dirty_board.removed[0]) == 3  # 1 zone + 1 track + 1 via
        assert dirty_board.commits_pushed == dirty_board.commits_begun
        assert dirty_board.save_count == 1

    def test_flags_off_removes_nothing(self, write_ops, dirty_board):
        result = write_ops.clean_board_for_routing(
            BOARD_PATH, remove_keepouts=False, remove_unassigned_tracks=False)
        assert result == {"status": "success",
                          "keepouts_removed": 0, "tracks_removed": 0}
        assert dirty_board.removed == []

    def test_netted_copper_untouched(self, write_ops, write_board):
        # default fixture: 1 GND track + 1 GND via, no rule areas
        result = write_ops.clean_board_for_routing(BOARD_PATH)
        assert result == {"status": "success",
                          "keepouts_removed": 0, "tracks_removed": 0}
        assert write_board.removed == []


class TestTextVariables:
    """Spec §3 rows 20–21 — kipy Project text variables via IPCBackend."""

    PRO_PATH = "D:/proj/test_board.kicad_pro"

    @pytest.fixture
    def project_recorder(self, live_board):
        from kipy.project_types import TextVariables
        from kipy.proto.common.types import project_settings_pb2

        stored = TextVariables(project_settings_pb2.TextVariables())
        stored.variables = {"REV": "B", "PROJECT": "AQS"}
        calls: dict = {"set": []}

        def set_text_variables(variables, merge_mode):
            calls["set"].append((dict(variables.variables), merge_mode))

        live_board.project = types.SimpleNamespace(
            name="test_board", path="D:/proj",
            get_text_variables=lambda: stored,
            set_text_variables=set_text_variables,
        )
        return calls

    @pytest.fixture
    def ipc_backend(self, live_board, monkeypatch):
        from kicad_mcp.backends import ipc_backend
        monkeypatch.setattr(ipc_backend, "_bridge_save", lambda path: True)
        return IPCBackend(FakeIPCBoardConnection(live_board))

    def test_get_shape(self, ipc_backend, project_recorder):
        result = ipc_backend.get_text_variables(self.PRO_PATH)
        assert result == {"status": "success",
                          "variables": {"REV": "B", "PROJECT": "AQS"}}

    def test_set_replace_semantics(self, ipc_backend, project_recorder):
        from kipy.proto.common.types import MapMergeMode
        result = ipc_backend.set_text_variables(self.PRO_PATH, {"REV": "C"})
        assert result == {"status": "success", "variables": {"REV": "C"},
                          "count": 1}
        assert project_recorder["set"] == [({"REV": "C"}, MapMergeMode.MMM_REPLACE)]

    def test_set_flushes_ipc_side_when_bridge_down(
            self, live_board, project_recorder, monkeypatch):
        from kicad_mcp.backends import ipc_backend
        monkeypatch.setattr(ipc_backend, "_bridge_save", lambda path: False)
        backend = IPCBackend(FakeIPCBoardConnection(live_board))
        backend.set_text_variables(self.PRO_PATH, {"REV": "C"})
        assert live_board.save_count == 1

    def test_project_mismatch_refused_with_canonical_phrase(
            self, ipc_backend, project_recorder):
        with pytest.raises(IPCUnavailableError) as exc_info:
            ipc_backend.get_text_variables("D:/other/wrong_project.kicad_pro")
        assert "does not match open project" in str(exc_info.value)

    def test_fresh_textvariables_proto_no_default_aliasing(
            self, ipc_backend, project_recorder):
        # kipy's TextVariables() default argument is a shared proto instance;
        # two sequential sets must not leak keys into each other.
        ipc_backend.set_text_variables(self.PRO_PATH, {"A": "1"})
        ipc_backend.set_text_variables(self.PRO_PATH, {"B": "2"})
        assert project_recorder["set"][1][0] == {"B": "2"}


class TestConnectionParams:
    def test_socket_path_env_override(self, fake_kipy, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_SOCKET", "ipc://custom/api.sock")
        conn = IPCConnection()
        conn.connect()
        assert fake_kipy.instances[-1].socket_path == "ipc://custom/api.sock"

    def test_explicit_socket_path_wins(self, fake_kipy, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_SOCKET", "ipc://env/api.sock")
        conn = IPCConnection(socket_path="ipc://explicit/api.sock")
        conn.connect()
        assert fake_kipy.instances[-1].socket_path == "ipc://explicit/api.sock"

    def test_timeout_env(self, fake_kipy, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_TIMEOUT_MS", "5000")
        conn = IPCConnection()
        conn.connect()
        assert fake_kipy.instances[-1].timeout_ms == 5000

    def test_bad_timeout_env_falls_back_to_default(self, fake_kipy, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_IPC_TIMEOUT_MS", "soon")
        conn = IPCConnection()
        conn.connect()
        assert fake_kipy.instances[-1].timeout_ms == 2000
