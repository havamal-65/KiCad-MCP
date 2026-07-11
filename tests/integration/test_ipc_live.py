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
    n_before = len(ops.get_components(path))

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

    # clean up the probe footprint via the bridge
    _tcp_call("remove_component", 10.0, path=str(path), reference="TIPC1")
