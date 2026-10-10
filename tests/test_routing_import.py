"""Tests for the TraceMaker routed-track import seam (Stage 1, Unit 2).

Covers:

1. ``extract_new_routes`` — added-only diff between the routing input copy and
   the routed output, net code→name mapping (KiCad ≤9), name-only passthrough
   (KiCad 10), via parsing, and the zero-routes hard refusal.
2. ``import_routed_tracks_with_fallback`` — bridge-success passthrough, the
   three fallback triggers (stale board / unreachable bridge / boardless
   bridge — same conditions as ``_import_ses_with_fallback``), the SHA256
   hash gate on the file-replace fallback, and unrelated-error re-raise.
3. Backend wiring — ``PluginBoardOps.add_routed_tracks`` TCP payload (mocked
   ``_tcp_call``) and the ``_LiveBoardOps`` IPC→bridge fallthrough.
4. The ``add_routed_tracks`` bridge handler — atomic all-or-nothing semantics
   against a fake pcbnew: success saves once; any offender rolls back every
   already-added item via ``RemoveNative`` and never saves.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.base import BoardOps
from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
    StaleBoardError,
)
from kicad_mcp.tools.routing_import import (
    extract_new_routes,
    import_routed_tracks_with_fallback,
)
from kicad_mcp_plugin.backends.plugin_direct import _LiveBoardOps


# ---------------------------------------------------------------------------
# Synthetic board fixtures
# ---------------------------------------------------------------------------

_SEG_EXISTING_CODED = (
    '  (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1))\n'
)
_SEG_EXISTING_NAMED = (
    '  (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net "GND"))\n'
)


def _coded_pcb(extra: str = "") -> str:
    """KiCad ≤9 convention: top-level net table + numeric net codes."""
    return (
        "(kicad_pcb\n"
        "  (version 20241229)\n"
        '  (generator "pcbnew")\n'
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (net 2 "VCC")\n'
        + _SEG_EXISTING_CODED
        + extra
        + ")\n"
    )


def _named_pcb(extra: str = "") -> str:
    """KiCad 10 convention: no net table, name-only (net "NAME") references."""
    return (
        "(kicad_pcb\n"
        "  (version 20260206)\n"
        '  (generator "pcbnew")\n'
        + _SEG_EXISTING_NAMED
        + extra
        + ")\n"
    )


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# extract_new_routes
# ---------------------------------------------------------------------------

def test_extract_added_only_diff(tmp_path: Path):
    """Only segments/vias absent from the input copy are extracted."""
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))\n'
        '  (segment (start 20 20) (end 30 20) (width 0.3) (layer "B.Cu") (net 2))\n'
        '  (via (at 20 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2))\n'
    ))

    result = extract_new_routes(inp, routed)

    assert len(result["segments"]) == 2
    assert len(result["vias"]) == 1
    # The pre-existing segment (10,10)→(20,10) must not reappear.
    starts = {(s["start_x"], s["start_y"]) for s in result["segments"]}
    assert (10.0, 10.0) not in starts


def test_extract_maps_net_codes_to_names(tmp_path: Path):
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))\n'
        '  (segment (start 20 20) (end 30 20) (width 0.3) (layer "B.Cu") (net 2))\n'
    ))

    result = extract_new_routes(inp, routed)

    nets = sorted(s["net"] for s in result["segments"])
    assert nets == ["GND", "VCC"]  # codes 1/2 resolved via the file's net table


def test_extract_net_code_zero_resolves_to_empty_name(tmp_path: Path):
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 0))\n'
    ))

    result = extract_new_routes(inp, routed)

    assert [s["net"] for s in result["segments"]] == [""]


def test_extract_name_only_passthrough(tmp_path: Path):
    """KiCad 10 files carry net names directly — no table, no mapping."""
    inp = _write(tmp_path / "in.kicad_pcb", _named_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _named_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net "GND"))\n'
        '  (via (at 20 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "VCC"))\n'
    ))

    result = extract_new_routes(inp, routed)

    assert [s["net"] for s in result["segments"]] == ["GND"]
    assert [v["net"] for v in result["vias"]] == ["VCC"]


def test_extract_via_payload_fields(tmp_path: Path):
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (via (at 25.5 30.25) (size 0.7) (drill 0.35) (layers "F.Cu" "B.Cu") (net 1))\n'
    ))

    result = extract_new_routes(inp, routed)

    assert result["segments"] == []
    assert result["vias"] == [{
        "x": 25.5, "y": 30.25, "size": 0.7, "drill": 0.35,
        "net": "GND", "layers": ["F.Cu", "B.Cu"],
    }]


def test_extract_identical_files_yield_empty_diff(tmp_path: Path):
    """Routed copy with routes but nothing NEW → empty diff, no error."""
    content = _coded_pcb()
    inp = _write(tmp_path / "in.kicad_pcb", content)
    routed = _write(tmp_path / "out.kicad_pcb", content)

    result = extract_new_routes(inp, routed)

    assert result == {"segments": [], "vias": []}


def test_extract_zero_routes_raises(tmp_path: Path):
    """A routed output with NO copper at all is a router failure, never an
    'identical' result (project lesson: assert you parsed something)."""
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", (
        "(kicad_pcb\n"
        "  (version 20241229)\n"
        '  (generator "pcbnew")\n'
        '  (net 0 "")\n'
        ")\n"
    ))

    with pytest.raises(ValueError, match="contains no routes"):
        extract_new_routes(inp, routed)


def test_extract_new_arc_in_routed_raises(tmp_path: Path):
    """The seam cannot carry (arc …) copper — an arc ADDED by the router must
    refuse fail-closed (the bridge payload would silently drop it)."""
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))\n'
        '  (arc (start 30 10) (mid 32 12) (end 34 10) (width 0.25) (layer "F.Cu") (net 1))\n'
    ))

    with pytest.raises(ValueError, match="arc tracks"):
        extract_new_routes(inp, routed)


def test_extract_preexisting_arcs_in_both_files_ok(tmp_path: Path):
    """Identical arcs already in BOTH files are untouched copper, not new —
    the guard must not fire and the segment diff still works."""
    arc = '  (arc (start 30 10) (mid 32 12) (end 34 10) (width 0.25) (layer "F.Cu") (net 1))\n'
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb(arc))
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        arc
        + '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))\n'
    ))

    result = extract_new_routes(inp, routed)

    assert len(result["segments"]) == 1
    assert result["vias"] == []


def test_extract_multiset_diff_counts_duplicate_copies(tmp_path: Path):
    """1 coincident segment in the input + 3 identical in the routed output →
    exactly 2 new (a plain set diff would lose both extra copies)."""
    dup = '  (segment (start 40 40) (end 50 40) (width 0.25) (layer "F.Cu") (net 1))\n'
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb(dup))
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(dup * 3))

    result = extract_new_routes(inp, routed)

    assert len(result["segments"]) == 2
    assert all(s["start_x"] == 40.0 for s in result["segments"])


def test_extract_via_missing_size_or_drill_raises(tmp_path: Path):
    """A via without explicit size/drill is a parse failure — geometry is
    never guessed."""
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (via (at 25.5 30.25) (size 0.7) (layers "F.Cu" "B.Cu") (net 1))\n'
    ))

    with pytest.raises(ValueError, match=r"via at \(25.5, 30.25\).*missing"):
        extract_new_routes(inp, routed)


def test_extract_unmapped_net_code_raises(tmp_path: Path):
    """A numeric code with no net-table entry must refuse, not guess."""
    inp = _write(tmp_path / "in.kicad_pcb", _coded_pcb())
    routed = _write(tmp_path / "out.kicad_pcb", _coded_pcb(
        '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 9))\n'
    ))

    with pytest.raises(ValueError, match="net code 9"):
        extract_new_routes(inp, routed)


# ---------------------------------------------------------------------------
# import_routed_tracks_with_fallback
# ---------------------------------------------------------------------------

_ROUTED_EXTRA = (
    '  (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))\n'
    '  (segment (start 20 20) (end 30 20) (width 0.25) (layer "B.Cu") (net 2))\n'
    '  (via (at 20 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))\n'
)


def _import_fixture(tmp_path: Path):
    """(board, input_copy, routed) with board byte-identical to input_copy."""
    content = _coded_pcb()
    board = _write(tmp_path / "board.kicad_pcb", content)
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    input_copy = _write(copy_dir / "board.kicad_pcb", content)
    routed = _write(copy_dir / "board_routed.kicad_pcb", _coded_pcb(_ROUTED_EXTRA))
    return board, input_copy, routed


def _backend(side):
    backend = MagicMock()
    if isinstance(side, Exception):
        backend.add_routed_tracks.side_effect = side
    else:
        backend.add_routed_tracks.return_value = side
    return backend


def test_bridge_success_path(tmp_path: Path):
    board, input_copy, routed = _import_fixture(tmp_path)
    before = board.read_bytes()
    backend = _backend({"status": "ok", "tracks_added": 2, "vias_added": 1})

    result = import_routed_tracks_with_fallback(backend, board, input_copy, routed)

    assert result == {"status": "ok", "tracks_added": 2, "vias_added": 1}
    backend.add_routed_tracks.assert_called_once()
    args = backend.add_routed_tracks.call_args.args
    assert args[0] == board
    assert len(args[1]) == 2 and len(args[2]) == 1   # the extracted diff
    assert args[1][0]["net"] == "GND"                # names, not codes
    assert board.read_bytes() == before              # bridge path never touches the file


@pytest.mark.parametrize("trigger", [
    StaleBoardError("disk newer than loaded", 200.0, 100.0),
    BridgeTemporarilyUnavailableError("bridge down"),
    RuntimeError("Requested board 'D:/p/b.kicad_pcb' does not match open board ''."),
], ids=["stale_board", "bridge_unreachable", "boardless_bridge"])
def test_fallback_triggers_replace_file_on_hash_match(tmp_path: Path, trigger):
    """Each of the three _import_ses_with_fallback conditions → file replace."""
    board, input_copy, routed = _import_fixture(tmp_path)
    backend = _backend(trigger)

    result = import_routed_tracks_with_fallback(backend, board, input_copy, routed)

    assert result["success"] is True
    assert result["via"] == "file_replace"
    assert result["new_tracks"] == 2
    assert result["new_vias"] == 1
    assert board.read_bytes() == routed.read_bytes()  # atomically replaced


def test_fallback_refuses_on_hash_mismatch(tmp_path: Path):
    """Board changed since the routing copy was made → RuntimeError, no write."""
    board, input_copy, routed = _import_fixture(tmp_path)
    # Mutate the live board file after the routing copy was taken.
    mutated = _coded_pcb('  (segment (start 50 50) (end 60 50) (width 0.2) (layer "F.Cu") (net 2))\n')
    _write(board, mutated)
    backend = _backend(BridgeTemporarilyUnavailableError("bridge down"))

    with pytest.raises(RuntimeError, match="changed since the routing copy"):
        import_routed_tracks_with_fallback(backend, board, input_copy, routed)

    assert board.read_text(encoding="utf-8") == mutated  # untouched


def test_unrelated_bridge_error_reraises_without_touching_file(tmp_path: Path):
    board, input_copy, routed = _import_fixture(tmp_path)
    before = board.read_bytes()
    backend = _backend(RuntimeError("add_routed_tracks failed — net 'X' not found"))

    with pytest.raises(RuntimeError, match="not found"):
        import_routed_tracks_with_fallback(backend, board, input_copy, routed)

    assert board.read_bytes() == before


def test_zero_route_output_fails_before_any_backend_call(tmp_path: Path):
    board, input_copy, _ = _import_fixture(tmp_path)
    empty_routed = _write(tmp_path / "copy" / "empty.kicad_pcb",
                          '(kicad_pcb (version 20241229) (generator "pcbnew") (net 0 ""))\n')
    backend = _backend({"status": "ok"})

    with pytest.raises(ValueError, match="contains no routes"):
        import_routed_tracks_with_fallback(backend, board, input_copy, empty_routed)

    backend.add_routed_tracks.assert_not_called()


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------

def test_plugin_board_ops_tcp_payload(tmp_path: Path):
    """PluginBoardOps.add_routed_tracks sends the method + segments/vias kwargs,
    path stringified — exactly the import_ses wiring pattern."""
    p = tmp_path / "b.kicad_pcb"
    segments = [{"start_x": 1.0, "start_y": 2.0, "end_x": 3.0, "end_y": 4.0,
                 "width": 0.25, "layer": "F.Cu", "net": "GND"}]
    vias = [{"x": 3.0, "y": 4.0, "size": 0.8, "drill": 0.4, "net": "GND",
             "layers": ["F.Cu", "B.Cu"]}]

    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call",
        return_value={"status": "ok", "tracks_added": 1, "vias_added": 1},
    ) as tcp:
        result = PluginBoardOps().add_routed_tracks(p, segments, vias)

    assert result == {"status": "ok", "tracks_added": 1, "vias_added": 1}
    tcp.assert_called_once()
    call = tcp.call_args
    assert call.args[0] == "add_routed_tracks"
    assert call.kwargs["path"] == str(p)
    assert call.kwargs["segments"] == segments
    assert call.kwargs["vias"] == vias


class _MinimalIPCOps(BoardOps):
    """IPC double without add_routed_tracks — the BoardOps base default raises
    NotImplementedError, which _LiveBoardOps must catch into the bridge path."""

    def read_board(self, path):
        return {}

    def get_components(self, path):
        return []

    def get_nets(self, path):
        return []

    def get_tracks(self, path):
        return []

    def get_board_info(self, path):
        return {}


class _BridgeDouble:
    def __init__(self):
        self.calls: list[tuple] = []

    def add_routed_tracks(self, path, segments, vias):
        self.calls.append((path, segments, vias))
        return {"status": "ok", "tracks_added": len(segments), "vias_added": len(vias)}


def test_live_board_ops_falls_through_ipc_to_bridge(tmp_path: Path):
    bridge = _BridgeDouble()
    recorded: list[str] = []
    ops = _LiveBoardOps(_MinimalIPCOps(), lambda: bridge, recorded.append)
    p = tmp_path / "b.kicad_pcb"
    segments = [{"start_x": 1.0, "start_y": 2.0, "end_x": 3.0, "end_y": 4.0,
                 "width": 0.25, "layer": "F.Cu", "net": "GND"}]

    result = ops.add_routed_tracks(p, segments, [])

    assert result["tracks_added"] == 1
    assert bridge.calls == [(p, segments, [])]
    assert recorded == ["bridge"]


# ---------------------------------------------------------------------------
# Bridge handler — atomic all-or-nothing semantics against a fake pcbnew
# ---------------------------------------------------------------------------

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location(
        "kicad_mcp_bridge_routed_tracks_under_test", BRIDGE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    """Stand-in for PCB_TRACK / PCB_VIA — records every setter call."""

    def __init__(self, board):
        self.props: dict = {}

    def __getattr__(self, name):
        if name.startswith("Set"):
            def setter(*args):
                self.props[name[3:]] = args if len(args) > 1 else args[0]
            return setter
        raise AttributeError(name)


class _FakeBoard:
    def __init__(self, filename: str, nets: dict):
        self._filename = filename
        self._nets = nets
        self.items: list = []
        self.removed: list = []
        self.saved: list = []

    def GetFileName(self):
        return self._filename

    def FindNet(self, name):
        return self._nets.get(name)

    def Add(self, item):
        self.items.append(item)

    def RemoveNative(self, item):
        self.items.remove(item)
        self.removed.append(item)

    def Save(self, filename):
        self.saved.append(filename)

    def GetLayerID(self, name):
        return -1  # anything beyond the _KNOWN table is unresolvable


def _fake_pcbnew(board) -> types.ModuleType:
    fake = types.ModuleType("pcbnew")
    fake.GetBoard = lambda: board
    fake.FromMM = lambda v: int(round(v * 1_000_000))
    fake.VECTOR2I = lambda x, y: (x, y)
    fake.PCB_TRACK = _FakeItem
    fake.PCB_VIA = _FakeItem
    fake.VIATYPE_THROUGH = 3
    fake.Refresh = lambda: None
    fake.F_Cu = 0
    fake.B_Cu = 31
    fake.F_SilkS = 37
    fake.B_SilkS = 36
    fake.Edge_Cuts = 44
    return fake


@pytest.fixture
def live_board(tmp_path: Path, monkeypatch, bridge):
    board_file = tmp_path / "live.kicad_pcb"
    board_file.write_text("(kicad_pcb)\n", encoding="utf-8")
    board = _FakeBoard(str(board_file), {"GND": object(), "VCC": object()})
    monkeypatch.setitem(sys.modules, "pcbnew", _fake_pcbnew(board))
    # Force the no-wx direct-execution path of _run_on_main_thread.
    monkeypatch.setitem(sys.modules, "wx", None)
    # Fresh coherence baseline per test.
    bridge._board_load_mtimes.clear()
    return board


_SEG_OK = {"start_x": 1.0, "start_y": 2.0, "end_x": 3.0, "end_y": 4.0,
           "width": 0.25, "layer": "F.Cu", "net": "GND"}
_VIA_OK = {"x": 3.0, "y": 4.0, "size": 0.8, "drill": 0.4, "net": "VCC",
           "layers": ["F.Cu", "B.Cu"]}


def test_bridge_handler_success_adds_all_and_saves_once(bridge, live_board):
    seg2 = dict(_SEG_OK, net="", layer="B.Cu")
    result = bridge._handle_add_routed_tracks(
        live_board.GetFileName(), [_SEG_OK, seg2], [_VIA_OK])

    assert result == {"status": "ok", "tracks_added": 2, "vias_added": 1}
    assert len(live_board.items) == 3
    assert live_board.saved == [live_board.GetFileName()]


def test_bridge_handler_unknown_net_rolls_back_everything(bridge, live_board):
    bad = dict(_SEG_OK, net="NOPE")
    with pytest.raises(ValueError, match="net 'NOPE' not found"):
        bridge._handle_add_routed_tracks(
            live_board.GetFileName(), [_SEG_OK, bad], [_VIA_OK])

    assert live_board.items == []        # the good segment was rolled back
    assert len(live_board.removed) == 1  # via RemoveNative, after being added
    assert live_board.saved == []        # a failed import never saves


def test_bridge_handler_unknown_layer_rolls_back_everything(bridge, live_board):
    bad_via = dict(_VIA_OK, layers=["F.Cu", "In7.Cu"])
    with pytest.raises(ValueError, match="In7.Cu"):
        bridge._handle_add_routed_tracks(
            live_board.GetFileName(), [_SEG_OK], [bad_via])

    assert live_board.items == []
    assert live_board.saved == []


def test_bridge_handler_short_via_layers_is_structured_offender(bridge, live_board):
    """layers shorter than [front, back] must be an offender-listed refusal
    (not an IndexError), with full rollback and zero saves."""
    bad_via = dict(_VIA_OK, layers=["F.Cu"])
    with pytest.raises(ValueError, match=r"via\[0\]: layers must be \[front, back\]"):
        bridge._handle_add_routed_tracks(
            live_board.GetFileName(), [_SEG_OK], [bad_via])

    assert live_board.items == []        # the good segment was rolled back
    assert len(live_board.removed) == 1  # via RemoveNative, after being added
    assert live_board.saved == []        # a failed import never saves


def test_bridge_handler_lists_all_offenders(bridge, live_board):
    bad_net = dict(_SEG_OK, net="NOPE")
    bad_layer = dict(_SEG_OK, layer="In9.Cu")
    with pytest.raises(ValueError) as excinfo:
        bridge._handle_add_routed_tracks(
            live_board.GetFileName(), [bad_net, bad_layer], [])

    msg = str(excinfo.value)
    assert "segment[0]" in msg and "NOPE" in msg
    assert "segment[1]" in msg and "In9.Cu" in msg
    assert live_board.saved == []


def test_bridge_handler_malformed_payload_rolls_back(bridge, live_board):
    """A payload dict missing a required key must not leave a partial batch on
    the live board (it would be persisted by the NEXT save of anything)."""
    with pytest.raises(KeyError):
        bridge._handle_add_routed_tracks(
            live_board.GetFileName(), [_SEG_OK, {"layer": "F.Cu", "net": ""}], [])

    assert live_board.items == []
    assert live_board.saved == []


def test_bridge_handler_registered_as_mutating(bridge):
    assert "add_routed_tracks" in bridge._DISPATCH
    assert "add_routed_tracks" in bridge._MUTATING_METHODS
    assert bridge._BRIDGE_VERSION == "2.8.0-tracemaker"
