"""KIR4 NS — KiCad-native sync + no-file-write invariant (spec §7).

Covers the netlist model, the FileBoardOps write guard, and sync v2's
contract: router-only mutations, netlist-driven nets (multi-unit correct),
structured refusals, and the verify block. Blocked-gate shapes and the
stub-removal refusals are additionally covered in the adapted suites
(test_sync_failure_suggestions, test_sync_schematic_to_pcb_gate,
test_file_backend, test_sync_footprint_swap, test_auto_place_clustering).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import fastmcp
import pytest

from tests.netlist_stub import DrcOpsStub, render_netlist

from kicad_mcp.backends.file_backend import (
    BoardOpenInKiCadError,
    FileBoardOps,
    _refuse_if_open_in_kicad,
)
from kicad_mcp.models.errors import CapabilityNotSupportedError, SafeRefuseError
from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.netlist_model import (
    NetlistParseError,
    parse_kicad_netlist,
)

# ---------------------------------------------------------------------------
# Netlist model (§7 cases 1–5)
# ---------------------------------------------------------------------------

_NETLIST = textwrap.dedent("""\
    (export (version "E")
      (components
        (comp (ref "R1") (value "10k") (footprint "Lib:R_0805"))
        (comp (ref "U1") (footprint "Lib:Valve"))
      )
      (nets
        (net (code "1") (name "GND")
          (node (ref "R1") (pin "2")) (node (ref "U1") (pin "3")))
        (net (code "2") (name "Net-(P1-PM)")
          (node (ref "U1") (pin "2")) (node (ref "U1") (pin "7")))
        (net (code "3") (name "unconnected-(P5-Pad1)")
          (node (ref "P5") (pin "1")))
      )
    )
""")


def _write_netlist(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "test.net"
    f.write_text(text, encoding="utf-8")
    return f


def test_parse_netlist_components_fields(tmp_path: Path):
    model = parse_kicad_netlist(_write_netlist(tmp_path, _NETLIST))
    assert [(c.reference, c.value, c.footprint) for c in model.components] == [
        ("R1", "10k", "Lib:R_0805"),
        ("U1", "", "Lib:Valve"),   # missing (value) -> ""
    ]


def test_parse_netlist_nets_nodes(tmp_path: Path):
    model = parse_kicad_netlist(_write_netlist(tmp_path, _NETLIST))
    assert model.nets[0].name == "GND"
    assert model.nets[0].nodes == (("R1", "2"), ("U1", "3"))
    assert model.nets[1].nodes == (("U1", "2"), ("U1", "7"))


def test_parse_netlist_multiunit_merged(tmp_path: Path):
    """U1's pins from different units appear under the single U1 ref."""
    model = parse_kicad_netlist(_write_netlist(tmp_path, _NETLIST))
    u1_pads = {p for (r, p) in model.pad_net_map() if r == "U1"}
    assert u1_pads == {"2", "3", "7"}


def test_pad_net_map_skips_unconnected_singletons(tmp_path: Path):
    model = parse_kicad_netlist(_write_netlist(tmp_path, _NETLIST))
    mapping = model.pad_net_map()
    assert ("P5", "1") not in mapping
    assert mapping[("R1", "2")] == "GND"
    assert mapping[("U1", "7")] == "Net-(P1-PM)"


def test_pad_net_map_keeps_multinode_unconnected_prefix(tmp_path: Path):
    text = _NETLIST.replace(
        '(net (code "3") (name "unconnected-(P5-Pad1)")\n'
        '      (node (ref "P5") (pin "1")))',
        '(net (code "3") (name "unconnected-(P5-Pad1)")\n'
        '      (node (ref "P5") (pin "1")) (node (ref "P6") (pin "1")))',
    )
    mapping = parse_kicad_netlist(_write_netlist(tmp_path, text)).pad_net_map()
    assert mapping[("P5", "1")] == "unconnected-(P5-Pad1)"


def test_parse_netlist_missing_sections_raise(tmp_path: Path):
    no_comp = _write_netlist(tmp_path, "(export (nets))")
    with pytest.raises(NetlistParseError, match="no \\(components\\) section"):
        parse_kicad_netlist(no_comp)
    no_nets = _write_netlist(tmp_path, "(export (components))")
    with pytest.raises(NetlistParseError, match="no \\(nets\\) section"):
        parse_kicad_netlist(no_nets)
    bad_comp = _write_netlist(
        tmp_path, '(export (components (comp (value "x"))) (nets))')
    with pytest.raises(NetlistParseError, match="without \\(ref\\)"):
        parse_kicad_netlist(bad_comp)


# ---------------------------------------------------------------------------
# File-backend write guard (§7 cases 16–20, 20d)
# ---------------------------------------------------------------------------

_BOARD = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (uuid "test-r1-uuid")
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
        (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
        (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
      )
      (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 0))
    )
""")


def _locked_board(tmp_path: Path) -> Path:
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_BOARD, encoding="utf-8")
    (tmp_path / "~b.kicad_pcb.lck").write_text(
        '{"hostname":"x","username":"y"}', encoding="utf-8")
    return board


def test_guard_refuses_lock_plus_kicad(tmp_path: Path):
    board = _locked_board(tmp_path)
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        with pytest.raises(BoardOpenInKiCadError) as exc_info:
            _refuse_if_open_in_kicad(board)
    assert "open in KiCad" in str(exc_info.value)
    assert exc_info.value.lock_file.name == "~b.kicad_pcb.lck"


def test_guard_allows_stale_lock(tmp_path: Path):
    board = _locked_board(tmp_path)
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=False):
        _refuse_if_open_in_kicad(board)  # no raise


def test_guard_allows_no_lock(tmp_path: Path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_BOARD, encoding="utf-8")
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        _refuse_if_open_in_kicad(board)  # no raise


_WRITE_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("place_component", ("R9", "Device:R", 5.0, 5.0)),
    ("move_component", ("R1", 20.0, 20.0)),
    ("remove_component", ("R1",)),
    ("add_track", (0.0, 0.0, 1.0, 1.0, 0.25)),
    ("add_via", (5.0, 5.0)),
    ("assign_net", ("R1", "1", "GND")),
    ("set_footprint_value", ("R1", "22k")),
    ("set_footprint_metadata", ("R1",), {"sheet_path": "/x"}),
    ("add_board_outline", (0.0, 0.0, 50.0, 50.0)),
    ("clear_routes", ()),
    ("place_components_bulk", ([{"reference": "R9", "footprint": "Device:R",
                                 "x": 5.0, "y": 5.0}],)),
]


@pytest.mark.parametrize(
    "method,args_extra", [(m, rest) for m, *rest in _WRITE_CALLS],
    ids=[m for m, *_ in _WRITE_CALLS],
)
def test_guard_on_every_write_method(tmp_path: Path, method, args_extra):
    """Every FileBoardOps board-write method refuses under lock+KiCad and
    leaves the file byte-identical (§5's complete method list)."""
    board = _locked_board(tmp_path)
    before = board.read_text(encoding="utf-8")
    args = args_extra[0] if args_extra else ()
    kwargs = args_extra[1] if len(args_extra) > 1 else {}
    ops = FileBoardOps()
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        with pytest.raises(BoardOpenInKiCadError):
            getattr(ops, method)(board, *args, **kwargs)
    assert board.read_text(encoding="utf-8") == before


def test_remove_component_board_scope_surfaces_guard_error(tmp_path: Path):
    board = _locked_board(tmp_path)
    mcp = fastmcp.FastMCP("test")
    backend = MagicMock()
    backend.get_board_modify_ops.return_value.remove_component.side_effect = \
        NotImplementedError  # force the file fallback
    schematic.register_tools(mcp, backend, ChangeLog(tmp_path / "c.json"))
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values()
                   if t.name == "remove_component")
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        result = json.loads(tool_fn(str(board), "R1", scope="pcb"))
    assert result["status"] == "error"
    assert result["error_code"] == "board_open_in_kicad"


def test_clear_routes_tool_surfaces_board_open_error(tmp_path: Path):
    from kicad_mcp.backends.plugin_backend import (
        BridgeTemporarilyUnavailableError,
    )
    from kicad_mcp.tools.routing import _impl_clear_routes

    board = _locked_board(tmp_path)
    backend = MagicMock()
    backend.get_board_modify_ops.return_value.clear_routes.side_effect = \
        BridgeTemporarilyUnavailableError("bridge down")
    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        result = json.loads(_impl_clear_routes(
            str(board), True, backend, ChangeLog(tmp_path / "c.json")))
    assert result["status"] == "error"
    assert result["error_code"] == "board_open_in_kicad"


# ---------------------------------------------------------------------------
# Bridge dispatch (§7 case 21)
# ---------------------------------------------------------------------------

def test_bridge_dispatch_has_set_footprint_metadata_and_mutating():
    import importlib.util

    bridge_path = (Path(__file__).parent.parent / "kicad_plugin"
                   / "kicad_mcp_bridge.py")
    spec = importlib.util.spec_from_file_location("kicad_mcp_bridge_ns",
                                                  bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert "set_footprint_metadata" in bridge._DISPATCH
    assert "set_footprint_metadata" in bridge._MUTATING_METHODS
    assert bridge._BRIDGE_VERSION == "2.6.0-sync"


# ---------------------------------------------------------------------------
# Sync v2 contract (§7 cases 6–15e)
# ---------------------------------------------------------------------------

_SYNC_NETLIST_COMPONENTS = [
    {"ref": "U1", "value": "ECC83", "footprint": "Test:Valve"},
]
_SYNC_NETLIST_NETS = [
    {"name": "GND", "nodes": [("U1", "3")]},
    {"name": "Net-A", "nodes": [("U1", "2"), ("U1", "7")]},
]


def _sync_harness(tmp_path: Path, *, drc_ops=None, modify_ops=None,
                  components_now=None, sch_symbols=None, get_modify_raises=None):
    """Registered sync tool + recording ops, footprint gate satisfied."""
    sch = tmp_path / "p.kicad_sch"
    pcb = tmp_path / "p.kicad_pcb"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    pcb.write_text('(kicad_pcb (version 20231231) (generator "t") (net 0 ""))',
                   encoding="utf-8")

    pretty = tmp_path / "Test.pretty"
    pretty.mkdir(exist_ok=True)
    (pretty / "Valve.kicad_mod").write_text("(footprint)", encoding="utf-8")

    backend = MagicMock()
    backend.get_drc_ops.return_value = drc_ops or DrcOpsStub(render_netlist(
        _SYNC_NETLIST_COMPONENTS, _SYNC_NETLIST_NETS))
    if get_modify_raises is not None:
        backend.get_board_modify_ops.side_effect = get_modify_raises
    elif modify_ops is not None:
        backend.get_board_modify_ops.return_value = modify_ops
    backend.get_board_ops.return_value.get_components.return_value = (
        components_now if components_now is not None else [])
    backend.get_schematic_ops.return_value.read_schematic.return_value = {
        "symbols": sch_symbols if sch_symbols is not None else []}

    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, backend, ChangeLog(tmp_path / "c.json"))
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values()
                   if t.name == "sync_schematic_to_pcb")

    def run(**kwargs):
        with patch("kicad_mcp.utils.fp_lib_table.get_footprint_library_map",
                   return_value={"Test": pretty}):
            return json.loads(tool_fn(str(sch), str(pcb), **kwargs))

    return run, backend, pcb


def _recording_ops():
    ops = MagicMock()
    ops.place_components_bulk.return_value = {
        "placed": ["U1"], "failed": [], "idempotent": []}
    ops.set_footprint_metadata.return_value = {
        "reference": "U1", "sheet_path_set": True, "fields_set": []}
    return ops


def test_sync_places_missing_via_bulk_ops(tmp_path: Path):
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    assert result["status"] == "success"
    ops.place_components_bulk.assert_called_once()
    batch = ops.place_components_bulk.call_args.args[1]
    assert batch == [{"reference": "U1", "footprint": "Test:Valve",
                      "x": 50.0, "y": 50.0}]
    assert result["summary"]["components_placed"] == 1


def test_sync_assigns_nets_from_netlist_only(tmp_path: Path):
    """The pad→net truth is KiCad's netlist — multi-unit pads land on their
    netlist nets (the recreation mis-net regression)."""
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    assigned = {(c.args[1], c.args[2]): c.args[3]
                for c in ops.assign_net.call_args_list}
    assert assigned == {("U1", "3"): "GND",
                        ("U1", "2"): "Net-A",
                        ("U1", "7"): "Net-A"}
    assert result["summary"]["nets_assigned"] == 3


def test_sync_values_for_placed_refs_unconditional(tmp_path: Path):
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    ops.set_footprint_value.assert_called_once()
    assert ops.set_footprint_value.call_args.args[1:] == ("U1", "ECC83")
    action = next(a for a in result["actions"] if a["type"] == "value_updated")
    assert action["old_value"] == "" and action["new_value"] == "ECC83"


def test_sync_metadata_via_ops_and_old_bridge_warning(tmp_path: Path):
    sch_symbols = [{"reference": "U1", "sheet_path": "/root-uuid",
                    "properties": {"PlacementIntent": "cluster:rf"}}]
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops,
                                      sch_symbols=sch_symbols)
    result = run()
    meta_call = ops.set_footprint_metadata.call_args
    assert meta_call.kwargs == {"sheet_path": "/root-uuid",
                                "fields": {"ClusterId": "rf"}}
    types = [a["type"] for a in result["actions"]]
    assert "metadata_set" in types and "cluster_assigned" in types

    # Old bridge: "Unknown method" -> bridge_upgrade_required warning.
    ops2 = _recording_ops()
    ops2.set_footprint_metadata.side_effect = RuntimeError(
        "Plugin bridge error: Unknown method: 'set_footprint_metadata'")
    run2, _, _ = _sync_harness(tmp_path, modify_ops=ops2,
                               sch_symbols=sch_symbols)
    result2 = run2()
    warn = next(w for w in result2["warnings"]
                if w["type"] == "bridge_upgrade_required")
    assert "2.6.0-sync" in warn["message"]


def _safe_refuse(msg: str) -> SafeRefuseError:
    return SafeRefuseError(
        msg, capability="board_write", remedy="open the board in pcbnew",
        paths_tried=["ipc", "bridge"],
    )


def test_sync_safe_refuse_at_acquisition(tmp_path: Path):
    run, backend, pcb = _sync_harness(
        tmp_path, get_modify_raises=_safe_refuse("KiCad open, no live path"))
    before = pcb.read_text(encoding="utf-8")
    result = run()
    assert result["status"] == "error"
    assert result["error_code"] == "live_path_required"
    assert pcb.read_text(encoding="utf-8") == before


def test_sync_safe_refuse_at_call_time_partial(tmp_path: Path):
    ops = _recording_ops()
    ops.place_components_bulk.side_effect = _safe_refuse("router refused")
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    assert result["error_code"] == "live_path_required"
    assert "partial" in result


def test_sync_placement_raise_returns_partial_error(tmp_path: Path):
    ops = _recording_ops()
    ops.place_components_bulk.side_effect = RuntimeError("bridge dropped")
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    assert result["error_code"] == "placement_failed"
    assert "re-run sync" in result["message"]
    assert "partial" in result


def test_sync_no_fileboardops_writes(tmp_path: Path):
    """The non-swap sync path never constructs FileBoardOps (REQ-NS-2,
    write-scope)."""
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    with patch.object(FileBoardOps, "__init__",
                      side_effect=AssertionError("FileBoardOps constructed")):
        result = run()
    assert result["status"] == "success"


def test_sync_verify_block_reports_mismatches(tmp_path: Path):
    """Mocked ops never touch the file -> every target pad is absent from the
    board read -> verify reports them all with actual=null."""
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    verified = result["verified"]
    assert verified["pads_expected"] == 3
    assert verified["pads_matched"] == 0
    assert verified["mismatches_total"] == 3
    assert all(m["actual"] is None for m in verified["mismatches"])


def test_sync_output_contract_keys(tmp_path: Path):
    ops = _recording_ops()
    run, backend, pcb = _sync_harness(tmp_path, modify_ops=ops)
    result = run()
    assert set(result.keys()) >= {
        "status", "summary", "actions", "verified",
        "footprint_changes_applied", "footprint_changes_skipped", "warnings",
    }
    assert set(result["summary"].keys()) == {
        "components_placed", "values_updated", "nets_assigned",
        "metadata_set", "footprint_changes_applied",
        "footprint_changes_skipped", "warnings",
    }


def test_sync_netlist_capability_error_structured(tmp_path: Path):
    run, backend, pcb = _sync_harness(tmp_path)
    backend.get_drc_ops.side_effect = CapabilityNotSupportedError(
        "kicad-cli not found")
    result = run()
    assert result["status"] == "error"
    assert result["error_code"] == "netlist_generation_failed"
    assert "kicad-cli" in result["message"]
