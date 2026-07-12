"""Live IPC-backend integration tests (F1 / S1 step 7 — T-IPC-2, T-COV-3/AC1).

Run against a real pcbnew with a board open (KICAD_INTEGRATION=1). The board
is treated as a throwaway scratch target like the rest of this suite.

Covers the #14 acceptance criterion: after an IN-PLACE mutation (the trigger
for the SWIG GetFileName detachment), board reads AND writes still succeed via
the IPC-first router; with IPC forced off, the same ops succeed via the bridge
fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kicad_mcp.backends.ipc_connection import IPCConnection
from kicad_mcp.backends.plugin_backend import _tcp_call


def _board_path() -> str:
    result = _tcp_call("get_active_project", 5.0)
    path = result["board_path"]
    assert path, "get_active_project returned empty board_path"
    return path


def _mutate_in_place(path: str) -> None:
    """Nudge a component via the bridge — the in-place mutation that can
    detach the SWIG board (#14). Position is restored immediately; the point
    is the mutation happening, not the move itself."""
    components = _tcp_call("get_components", 10.0, path=path)
    assert components, "board has no components to mutate"
    ref = components[0]["reference"]
    x, y, rot = components[0]["x"], components[0]["y"], components[0]["rotation"]
    _tcp_call("move_component", 10.0, path=path, reference=ref,
              x=x + 0.1, y=y, rotation=rot)
    _tcp_call("move_component", 10.0, path=path, reference=ref,
              x=x, y=y, rotation=rot)


@pytest.fixture(scope="module")
def ipc_session(bridge_session):
    """Gates IPC-leg tests on a reachable IPC server with a loaded board."""
    if os.environ.get("KICAD_MCP_IPC_ENABLED", "1").lower() in ("0", "false"):
        pytest.skip("KICAD_MCP_IPC_ENABLED=0 — IPC leg disabled for this run")
    conn = IPCConnection()
    if not conn.is_available():
        pytest.skip(
            "KiCad IPC API not reachable with a loaded board. Enable "
            "Preferences → Plugins → IPC API server and restart KiCad."
        )
    return conn


# ---------------------------------------------------------------------------
# T-IPC-2 (REQ-IPC-2) — the exact #14 datum
# ---------------------------------------------------------------------------

def test_board_name_readable_after_inplace_mutation(bridge_session, ipc_session):
    """board.name (the datum the SWIG bridge loses) survives in-place mutation."""
    path = _board_path()
    _mutate_in_place(path)
    name = ipc_session.board().name
    assert name == Path(path).name


# ---------------------------------------------------------------------------
# T-COV-3 / AC1 (REQ-COV-3) — reads AND writes survive via the router
# ---------------------------------------------------------------------------

def test_ipc_reads_and_writes_survive_inplace_mutation(bridge_session, ipc_session):
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

    path = Path(_board_path())
    _mutate_in_place(str(path))

    backend = PluginDirectBackend()

    # reads — served over IPC, no GetFileName hard stop
    ops = backend.get_board_ops()
    info = ops.get_board_info(path)
    assert info["footprint_count"] > 0
    components = ops.get_components(path)
    assert components
    assert backend._live_path["BOARD_READ"] == "ipc"

    # write — a track lands on the live board and is verified by re-read
    modify = backend.get_board_modify_ops()
    result = modify.add_track(path, 201.0, 201.0, 203.0, 201.0, 0.25, layer="F.Cu")
    assert result["status"] == "ok"
    assert backend._live_path["BOARD_MODIFY"] == "ipc"
    tracks = ops.get_tracks(path)
    added = [t for t in tracks if t.get("start_x") == 201.0 and t.get("start_y") == 201.0]
    assert len(added) == 1

    # active project — the #14 symptom call — served via IPC
    active = backend.get_active_project()
    assert active["board_path"]
    assert backend._live_path["ACTIVE_PROJECT"] == "ipc"


def test_bridge_fallback_serves_same_ops_with_ipc_forced_off(
    bridge_session, ipc_session, monkeypatch,
):
    from kicad_mcp.backends.plugin_backend import PluginBoardOps
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

    monkeypatch.setenv("KICAD_MCP_IPC_ENABLED", "0")
    path = Path(_board_path())
    backend = PluginDirectBackend()

    # same read now served by the bridge
    ops = backend.get_board_ops()
    assert isinstance(ops, PluginBoardOps)
    info = ops.get_board_info(path)
    assert info["footprint_count"] > 0
    assert backend._live_path["BOARD_READ"] == "bridge"

    # same write lands via the bridge
    modify = backend.get_board_modify_ops()
    result = modify.add_track(path, 201.0, 205.0, 203.0, 205.0, 0.25, layer="F.Cu")
    assert result["status"] == "ok"
    tracks = ops.get_tracks(path)
    added = [t for t in tracks if t.get("start_x") == 201.0 and t.get("start_y") == 205.0]
    assert len(added) == 1
    assert backend._live_path["BOARD_MODIFY"] == "bridge"


def test_per_op_fallback_place_component_lands_real_footprint(
    bridge_session, ipc_session,
):
    """REQ-ROUTE-4 live: place_component enters via IPC, the empty-definition
    guard refuses, the proxy retries on the bridge, and a REAL footprint (with
    pads) lands on the live board."""
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

    path = Path(_board_path())
    backend = PluginDirectBackend()
    ops = backend.get_board_ops()

    def _remove_probe() -> None:
        try:
            _tcp_call("remove_component", 10.0, path=str(path), reference="TIPC1")
        except RuntimeError:
            pass  # not on board

    _remove_probe()  # self-heal from a crashed prior run (REQ-FIX-1)
    n_before = len(ops.get_components(path))
    try:
        modify = backend.get_board_modify_ops()
        result = modify.place_component(
            path, "TIPC1", "Resistor_SMD:R_0805_2012Metric", 205.0, 205.0)
        assert result["status"] == "ok"
        assert backend._live_path["BOARD_MODIFY"] == "bridge"  # fell through

        components = ops.get_components(path)
        assert len(components) == n_before + 1
        placed = [c for c in components if c["reference"] == "TIPC1"]
        assert len(placed) == 1

        # pads present = the bridge materialized the real library definition
        pads = ipc_session.board().get_footprints()
        target = [f for f in pads if f.reference_field.text.value == "TIPC1"]
        assert target and len(target[0].definition.pads) == 2
    finally:
        # clean up the probe footprint via the bridge
        _remove_probe()


# ---------------------------------------------------------------------------
# S2 — full 21-tool live matrix (REQ-COV-1)
# ---------------------------------------------------------------------------

def test_full_21_tool_live_matrix(bridge_session, ipc_session):
    """REQ-COV-1 (S2): on a real session every live-board tool's backend op
    resolves to IPC or its documented fallback, and the served path telemetry
    proves it.

    Rows not exercised directly here, with their documented resolution:
    - ``place_component`` — bridge (per-op fall; dedicated test above)
    - ``place_at_edge`` — rides move/place (F2 owns the edge marker)
    - ``verify_board_size`` — rides the board reads
    - ``set_board_design_rules`` — file-side by design (pcbnew-clobber
      contract, d018367); never called against a live session
    """
    from kicad_mcp.tools.drc import _parse_board_bbox
    from kicad_mcp.utils.placement_engine import read_part_records
    from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

    path = Path(_board_path())
    backend = PluginDirectBackend()
    ops = backend.get_board_ops()
    modify = backend.get_board_modify_ops()
    matrix: dict[str, str] = {}

    def record(tool: str, cap: str = "BOARD_MODIFY") -> None:
        matrix[tool] = backend._live_path[cap]

    # -- reads (rows 1–4, 13–14) ------------------------------------------
    assert ops.read_board(path)["info"]
    record("read_board", "BOARD_READ")
    assert ops.get_board_info(path)["footprint_count"] > 0
    record("get_board_info", "BOARD_READ")
    rules = ops.get_design_rules(path)
    assert any(k.endswith("_mm") for k in rules), rules
    record("get_design_rules", "BOARD_READ")
    stackup = backend.get_board_stackup_ops().get_stackup(path)
    assert stackup["layers"], stackup
    record("get_stackup", "BOARD_STACKUP")
    assert backend.get_active_project()["board_path"]
    matrix["get_active_project"] = backend._live_path["ACTIVE_PROJECT"]

    # -- text variables (rows 20–21), restored afterwards ------------------
    pro_path = str(path.with_suffix(".kicad_pro"))
    original_vars = backend.get_text_variables(pro_path)["variables"]
    record("get_text_variables", "TEXT_VARS")
    try:
        backend.set_text_variables(pro_path, {"S2_MATRIX": "on"})
        record("set_text_variables", "TEXT_VARS")
        echoed = backend.get_text_variables(pro_path)["variables"]
        assert echoed == {"S2_MATRIX": "on"}, echoed
    finally:
        backend.set_text_variables(pro_path, dict(original_vars))

    # -- copper writes (rows 6, 9–12) --------------------------------------
    comps = ops.get_components(path)
    first = comps[0]
    modify.move_component(path, first["reference"], first["x"], first["y"],
                          rotation=first["rotation"])
    record("move_component")
    modify.add_track(path, 210.0, 210.0, 212.0, 210.0, 0.25)
    record("add_track")
    modify.add_via(path, 211.0, 211.0)
    record("add_via")

    fps = [f for f in ipc_session.board().get_footprints() if f.definition.pads]
    netted = [(f, p) for f in fps for p in f.definition.pads if p.net.name]
    if netted:
        fp, pad = netted[0]
        modify.assign_net(path, fp.reference_field.text.value,
                          pad.number, pad.net.name)  # reassign = no-op write
        record("assign_net")

    modify.clear_routes(path, backup=False)
    record("clear_routes")

    # -- outline (row 11): re-add the same bbox — an idempotent replace ----
    bbox = _parse_board_bbox(path.read_text(encoding="utf-8"))
    assert bbox is not None
    modify.add_board_outline(path, bbox[0], bbox[1],
                             bbox[2] - bbox[0], bbox[3] - bbox[1])
    record("add_board_outline")

    # -- specialized (rows 16–19) ------------------------------------------
    zone_ops = backend.get_zone_refill_ops()
    assert zone_ops is not None
    zone_ops.refill_zones(path)
    record("refill_zones", "ZONE_REFILL")
    modify.clean_board_for_routing(path)  # sweeps the net-less test copper
    record("clean_board_for_routing")

    modify.save_board(path)
    record("save_project")
    snapshot = {r["ref"]: r["pos"]
                for r in read_part_records(path.read_text(encoding="utf-8"))}
    try:
        result = modify.auto_place(
            path, bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1],
            clearance_mm=0.5, strategy="net_aware")
        assert result["strategy"] == "net_aware"
        record("auto_place")
    finally:
        for ref, (ox, oy, orot) in sorted(snapshot.items()):
            modify.move_component(path, ref, ox, oy, rotation=orot)
        modify.save_board(path)

    # -- the verdict --------------------------------------------------------
    expected_ipc = {
        "read_board", "get_board_info", "get_design_rules", "get_stackup",
        "get_active_project", "get_text_variables", "set_text_variables",
        "move_component", "add_track", "add_via", "clear_routes",
        "add_board_outline", "refill_zones", "clean_board_for_routing",
        "save_project", "auto_place",
    }
    if netted:
        expected_ipc.add("assign_net")
    wrong = {t: p for t, p in matrix.items()
             if t in expected_ipc and p != "ipc"}
    assert not wrong, f"tools not served over IPC: {wrong} (matrix: {matrix})"
