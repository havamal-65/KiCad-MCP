"""REQ-EDGE-5 / AC3 (F2/S3, #18) — USB4085-class connector end-to-end, live.

The GCT USB4085 horizontal USB-C receptacle has a 2.51 mm datasheet overhang
past the board edge. Before #18 the placement-quality gate blocked it as
``out_of_outline`` and autoroute refused. This test proves the full chain on
the live scratch board: **places** (bridge), edge-anchors with
``compute_edge_placement`` (PCB-Edge marker datum when the stock footprint
carries one), **passes the gate** (overhang exempt, nothing else masked), and
**autoroutes** a net end-to-end through the real FreeRouting pipeline.

Skips cleanly when the stock Connector_USB library, Java, or the FreeRouting
jar is unavailable. Everything the test adds is removed/restored in finally
(REQ-FIX-1); the session guard byte-restores the fixture afterwards anyway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call

pytestmark = pytest.mark.integration

_USB4085_FP = "Connector_USB:USB_C_Receptacle_GCT_USB4085"
_REF = "T15_J1"
_NET = "T15_NET"


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def _remove_if_present(path: str, reference: str) -> None:
    try:
        _tcp_call("remove_component", 10.0, path=path, reference=reference)
    except RuntimeError:
        pass


def _first_pad_number(content: str, ref: str) -> str | None:
    """First named pad of *ref* — the stock USB4085 is the THT-signal
    variant, so accept any pad type (thru_hole/smd)."""
    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference
    located = find_footprint_block_by_reference(content, ref)
    if located is None:
        return None
    block = content[located[0]:located[1] + 1]
    m = re.search(r'\(pad\s+"([^"]+)"\s+\w+', block)
    return m.group(1) if m and m.group(1) else None


def test_usb4085_places_gates_and_autoroutes(bridge_session):
    from kicad_mcp.backends.file_backend import _load_kicad_mod
    from kicad_mcp.config import KiCadMCPConfig
    from kicad_mcp.tools.drc import (
        _parse_board_bbox,
        compute_edge_placement,
        run_validate_connector_orientations,
        run_validate_placement_quality,
    )
    from kicad_mcp.utils.placement_engine import read_part_records
    from kicad_mcp.utils.platform_helper import (
        detect_java_major_version,
        find_freerouting_jar,
        find_java,
    )

    if _load_kicad_mod(_USB4085_FP) is None:
        pytest.skip(f"{_USB4085_FP} not available on this KiCad install")
    java = find_java()
    if java is None:
        pytest.skip("Java not available — FreeRouting leg cannot run")
    jar = find_freerouting_jar(detect_java_major_version(java))
    if jar is None or not jar.exists():
        pytest.skip("FreeRouting jar not found (no auto-download in tests)")

    path = _board_path()
    p = Path(path)
    content = p.read_text(encoding="utf-8")

    # Pose snapshot of everything already on the board — restored in finally.
    snapshot = {r["ref"]: r["pos"] for r in read_part_records(content)}

    _remove_if_present(path, _REF)
    dsn = p.parent / "freerouting.dsn"
    ses = p.parent / "freerouting.ses"
    try:
        # -- stage a known-clean board state --------------------------------
        # Earlier suite tests legitimately mangle the shared scratch board
        # (the outline test shrinks Edge.Cuts, the row auto_place re-packs
        # every footprint), so build our own stage: a fresh 80x70 outline and
        # tidy poses clear of the ESP32 antenna keep-out. The session guard
        # byte-restores the fixture afterwards; this test runs last.
        _tcp_call("add_board_outline", 15.0, path=path,
                  x=0.0, y=0.0, width=80.0, height=70.0)
        xmin, ymin, xmax, ymax = 0.0, 0.0, 80.0, 70.0
        stage = {
            "T10_U1": (40.0, 35.0, 0.0),
            "T14_R1": (10.0, 10.0, 0.0),
            "T14_R2": (16.0, 10.0, 0.0),
            "T14_R3": (22.0, 10.0, 0.0),
            "T14_R4": (28.0, 10.0, 0.0),
        }
        for ref, (sx, sy, srot) in stage.items():
            try:
                _tcp_call("move_component", 10.0, path=path, reference=ref,
                          x=sx, y=sy, rotation=srot)
            except RuntimeError:
                pass  # ref not on this board — staging is best-effort
        # -- place (interior first; compute_edge_placement reads the board) --
        try:
            _tcp_call(
                "place_component", 10.0,
                path=path, reference=_REF, footprint=_USB4085_FP,
                x=(xmin + xmax) / 2.0, y=(ymin + ymax) / 2.0, rotation=0,
            )
        except RuntimeError as exc:
            if "Could not find library" in str(exc) or "not found in" in str(exc):
                pytest.skip(f"{_USB4085_FP} not resolvable inside pcbnew: {exc}")
            raise

        # -- edge-anchor at south (marker datum when the footprint has one) --
        plan = compute_edge_placement(p, _REF, "south")
        assert plan["status"] == "success", plan
        # Keep clear of the fixture parts (T14s bottom-left, ESP32 mid-left):
        # slide along the south edge to 3/4 board width.
        target_x = xmin + (xmax - xmin) * 0.75
        _tcp_call("move_component", 10.0, path=path, reference=_REF,
                  x=target_x, y=plan["target_y"],
                  rotation=plan["target_rotation"])

        # -- give it something to route BEFORE the gates: the gate cache is
        # keyed on board state, so the net assignments must precede the
        # validations or autoroute sees a cache miss and refuses.
        board_text = p.read_text(encoding="utf-8")
        usb_pad = _first_pad_number(board_text, _REF)
        assert usb_pad, "no pad found on the placed USB4085"
        _tcp_call("assign_net", 10.0, path=path, reference=_REF,
                  pad=usb_pad, net=_NET)
        _tcp_call("assign_net", 10.0, path=path, reference="T14_R1",
                  pad="1", net=_NET)

        # -- gates (AC3: "passes the gate") -------------------------------
        orient = run_validate_connector_orientations(p)
        assert orient["passed"] is True, orient["violations"]

        quality = run_validate_placement_quality(p)
        out_violations = [
            v for v in quality["violations"] if v["type"] == "out_of_outline"
        ]
        assert out_violations == [], (
            f"USB4085 overhang not exempted (datum={plan['datum']}): "
            f"{quality['violations']}"
        )
        assert quality["passed"] is True, quality["violations"]

        import fastmcp
        from kicad_mcp.tools import routing
        from kicad_mcp.utils.change_log import ChangeLog
        from kicad_mcp_plugin.backends.plugin_direct import PluginDirectBackend

        backend = PluginDirectBackend()
        mcp = fastmcp.FastMCP("test")
        routing.register_tools(
            mcp, backend, ChangeLog(p.parent / "changes.json"), KiCadMCPConfig())
        autoroute_fn = next(
            t.fn for t in mcp._tool_manager._tools.values() if t.name == "autoroute"
        )
        # clean_board=False: the default sweep would strip the fixture's
        # embedded ESP32 antenna keep-out (a rule area) from the live board.
        report = json.loads(autoroute_fn(path, max_passes=2, clean_board=False))
        # The scratch fixture is a torture board full of netless pads, so the
        # pipeline may report "success", "success_with_drc_errors", or (with the
        # F3 honest reporting) "partial" when some torture-board nets stay
        # unrouted. Any of these means the routing pipeline ran end-to-end; the
        # copper assertion below is the real AC3 check.
        assert str(report["status"]) in (
            "success", "success_with_drc_errors", "partial",
        ), report
        step_names = [s["step"] for s in report["steps"]]
        assert "run_freerouter" in step_names, step_names

        # The net actually landed as copper: a routed track carries T15_NET.
        routed = p.read_text(encoding="utf-8")
        net_m = re.search(rf'\(net\s+(\d+)\s+"{_NET}"\)', routed)
        assert net_m, "net vanished after autoroute"
        assert re.search(
            rf'\(segment\s.*?\(net\s+{net_m.group(1)}\)', routed, re.DOTALL,
        ), "no routed segment on the USB4085 net — autoroute did not route it"
    finally:
        try:
            _tcp_call("clear_routes", 15.0, path=path, backup=False)
        except RuntimeError:
            pass
        _remove_if_present(path, _REF)
        for ref, (ox, oy, orot) in sorted(snapshot.items()):
            try:
                _tcp_call("move_component", 10.0, path=path, reference=ref,
                          x=ox, y=oy, rotation=orot)
            except RuntimeError:
                pass
        dsn.unlink(missing_ok=True)
        ses.unlink(missing_ok=True)
