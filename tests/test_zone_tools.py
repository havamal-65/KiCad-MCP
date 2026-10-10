"""spec-zones Z1-Z5 — add_zone / list_zones / delete_zone / refill_zones.

Layers, all KiCad-free (mock at the ops/subprocess boundary per src/CLAUDE.md):
- the tools driving the REAL FileBoardOps on real board text (file path)
- structured refusals (SafeRefuse, board-open, selector validation)
- the Z4 subprocess helper contract (generated script pinned, no live run)
- PluginBoardOps TCP serialization for add_zone/delete_zone
- bridge dispatch registration + the zoneless no-save rule (handler run with
  the main-thread hop and board stubbed out)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps, parse_board_zones
from kicad_mcp.backends.plugin_backend import PluginBoardOps
from kicad_mcp.models.errors import SafeRefuseError
from kicad_mcp.tools import board as board_tools
from kicad_mcp.utils import zone_fill_subprocess as zfs
from kicad_mcp.utils.change_log import ChangeLog

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


# ── fixtures ─────────────────────────────────────────────────────────────────

BOARD_CODED = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "GND")
      (net 2 "VCC")
      (gr_rect (start 0 0) (end 60 40)
        (stroke (width 0.05) (type solid))
        (layer "Edge.Cuts")
        (uuid "edge-1")
      )
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
      )
    )
""")

BOARD_NAME_ONLY = textwrap.dedent("""\
    (kicad_pcb
      (version 20260206)
      (generator "pcbnew")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND"))
      )
    )
""")

COPPER_ZONE = textwrap.dedent("""\
      (zone
        (net 1)
        (net_name "GND")
        (layer "B.Cu")
        (uuid "zone-copper-1")
        (name "gnd_pour")
        (hatch edge 0.5)
        (connect_pads
          (clearance 0.5)
        )
        (min_thickness 0.25)
        (fill
          (thermal_gap 0.5)
          (thermal_bridge_width 0.5)
        )
        (polygon
          (pts
            (xy 1 1) (xy 59 1) (xy 59 39) (xy 1 39)
          )
        )
      )
""")

RULE_AREA_ZONE = textwrap.dedent("""\
      (zone
        (net 0)
        (net_name "")
        (layers "F.Cu" "B.Cu")
        (uuid "zone-keepout-1")
        (name "antenna_keepout")
        (hatch edge 0.5)
        (keepout
          (tracks not_allowed)
          (vias not_allowed)
          (pads allowed)
          (copperpour not_allowed)
          (footprints allowed)
        )
        (polygon
          (pts
            (xy 5 5) (xy 10 5) (xy 10 10)
          )
        )
      )
""")


def _board(tmp_path: Path, content: str = BOARD_CODED,
           zones: str = "") -> Path:
    p = tmp_path / "test.kicad_pcb"
    if zones:
        content = content[:content.rfind(")")] + zones + ")\n"
    p.write_text(content, encoding="utf-8")
    return p


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(tmp_path: Path, backend: Any = None) -> dict:
    if backend is None:
        backend = MagicMock()
        ops = FileBoardOps()
        backend.get_zone_create_ops.return_value = ops
        backend.get_zone_refill_ops.return_value = ops
    mcp = _CapturingMCP()
    board_tools.register_tools(mcp, backend, ChangeLog(tmp_path / "cl.json"))
    return mcp.tools


_NO_FILL = {"filled": False, "reason": "test: fill disabled"}


# ── Z1 add_zone (file path) ──────────────────────────────────────────────────

def test_add_zone_file_writes_zone_sexp(tmp_path: Path) -> None:
    board = _board(tmp_path)
    tools = _tools(tmp_path)
    outline = [{"x": 2, "y": 2}, {"x": 58, "y": 2}, {"x": 58, "y": 38}]

    result = json.loads(tools["add_zone"](
        path=str(board), net="GND", layer="B.Cu", outline=outline,
        name="gnd_pour", refill=False,
    ))

    assert result["status"] == "success"
    assert result["via"] == "file"
    assert result["filled"] is False
    assert result["outline_points"] == 3
    assert result["warnings"], "unfilled zone must carry a warning"
    content = board.read_text(encoding="utf-8")
    assert "(net 1)" in content
    assert '(net_name "GND")' in content
    assert '(layer "B.Cu")' in content
    assert '(name "gnd_pour")' in content
    assert "(clearance 0.5)" in content
    assert "(min_thickness 0.25)" in content
    assert "(filled_areas_thickness no)" in content
    assert "(xy 2 2) (xy 58 2) (xy 58 38)" in content
    # The file round-trips through the Z2 parser.
    zones = parse_board_zones(content)
    assert len(zones) == 1 and zones[0]["net"] == "GND"


def test_add_zone_name_only_file_uses_k10_net_clause(tmp_path: Path) -> None:
    board = _board(tmp_path, content=BOARD_NAME_ONLY)
    tools = _tools(tmp_path)

    result = json.loads(tools["add_zone"](
        path=str(board), net="GND",
        outline=[{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}],
        refill=False,
    ))

    assert result["status"] == "success"
    content = board.read_text(encoding="utf-8")
    # KiCad-10 grammar (measured 2026-09-13): name-only net, no net_name,
    # no filled_areas_thickness.
    zone_block = content[content.find("(zone"):]
    assert '(net "GND")' in zone_block
    assert "net_name" not in zone_block
    assert "filled_areas_thickness" not in zone_block


def test_add_zone_outline_validation(tmp_path: Path) -> None:
    board = _board(tmp_path)
    tools = _tools(tmp_path)
    pts = [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}]

    both = json.loads(tools["add_zone"](
        path=str(board), net="GND", outline=pts, follow_board_outline=True))
    assert both["error_code"] == "zone_outline_conflict"
    assert "not both" in both["message"]

    neither = json.loads(tools["add_zone"](path=str(board), net="GND"))
    assert neither["error_code"] == "zone_outline_conflict"
    assert "provide outline points or set follow_board_outline" in neither["message"]

    too_few = json.loads(tools["add_zone"](
        path=str(board), net="GND", outline=pts[:2]))
    assert too_few["error_code"] == "zone_outline_invalid"
    assert "at least 3 points" in too_few["message"]


def test_add_zone_net_not_found(tmp_path: Path) -> None:
    board = _board(tmp_path)
    tools = _tools(tmp_path)

    result = json.loads(tools["add_zone"](
        path=str(board), net="NOPE",
        outline=[{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}],
        refill=False,
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "net_not_found"
    assert "run sync_schematic_to_pcb first" in result["message"]
    assert "(zone" not in board.read_text(encoding="utf-8")


def test_add_zone_follow_board_outline_inset_bbox(tmp_path: Path) -> None:
    board = _board(tmp_path)
    tools = _tools(tmp_path)

    result = json.loads(tools["add_zone"](
        path=str(board), net="GND", follow_board_outline=True,
        outline_margin_mm=0.5, refill=False,
    ))

    assert result["status"] == "success"
    assert result["outline_points"] == 4
    content = board.read_text(encoding="utf-8")
    # Edge.Cuts bbox (0,0)-(60,40) inset by 0.5 on all sides.
    assert "(xy 0.5 0.5) (xy 59.5 0.5) (xy 59.5 39.5) (xy 0.5 39.5)" in content


def test_add_zone_no_board_outline(tmp_path: Path) -> None:
    no_edge = BOARD_CODED.replace('(layer "Edge.Cuts")', '(layer "F.Fab")')
    board = _board(tmp_path, content=no_edge)
    tools = _tools(tmp_path)

    result = json.loads(tools["add_zone"](
        path=str(board), net="GND", follow_board_outline=True))
    assert result["error_code"] == "no_board_outline"


def test_add_zone_pad_connection_mapping(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    pts = [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}]

    for mode, token in (("thermal", "(connect_pads\n"),
                        ("solid", "(connect_pads yes\n"),
                        ("none", "(connect_pads no\n")):
        board = tmp_path / f"{mode}.kicad_pcb"
        board.write_text(BOARD_CODED, encoding="utf-8")
        result = json.loads(tools["add_zone"](
            path=str(board), net="GND", outline=pts,
            pad_connection=mode, refill=False,
        ))
        assert result["status"] == "success", mode
        content = board.read_text(encoding="utf-8").replace("\t", "")
        assert token in content, (mode, token)

    board = _board(tmp_path)
    bad = json.loads(tools["add_zone"](
        path=str(board), net="GND", outline=pts, pad_connection="direct"))
    assert bad["error_code"] == "invalid_pad_connection"
    assert bad["message"] == "pad_connection must be one of: thermal, solid, none"


def test_add_zone_refuses_when_board_open(tmp_path: Path) -> None:
    board = _board(tmp_path)
    (tmp_path / f"~{board.name}.lck").write_text("", encoding="utf-8")
    tools = _tools(tmp_path)

    with patch("kicad_mcp.utils.platform_helper.is_kicad_running",
               return_value=True):
        result = json.loads(tools["add_zone"](
            path=str(board), net="GND",
            outline=[{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}],
            refill=False,
        ))

    assert result["status"] == "error"
    assert result["error_code"] == "board_open_in_kicad"


def test_add_zone_saferefuse_structured(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.get_zone_create_ops.side_effect = SafeRefuseError(
        "no live path", capability="ZONE_CREATE",
        remedy="open the board in the PCB editor", paths_tried=["ipc", "bridge"],
    )
    board = _board(tmp_path)
    tools = _tools(tmp_path, backend=backend)

    result = json.loads(tools["add_zone"](
        path=str(board), net="GND",
        outline=[{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}],
    ))
    assert result["error_code"] == "kicad_running_no_live_path"
    assert result["remedy"] == "open the board in the PCB editor"


# ── Z2 list_zones ────────────────────────────────────────────────────────────

def test_list_zones_reports_copper_and_rule_areas(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE + RULE_AREA_ZONE)
    tools = _tools(tmp_path)

    result = json.loads(tools["list_zones"](path=str(board)))

    assert result["status"] == "success"
    assert result["source"] == "file"
    assert result["zone_count"] == 2
    copper = next(z for z in result["zones"] if not z["is_rule_area"])
    keepout = next(z for z in result["zones"] if z["is_rule_area"])
    assert copper == {
        "name": "gnd_pour", "net": "GND", "layers": ["B.Cu"],
        "is_rule_area": False, "filled": False, "outline_points": 4,
        "uuid": "zone-copper-1",
    }
    assert keepout["name"] == "antenna_keepout"
    assert keepout["layers"] == ["F.Cu", "B.Cu"]
    assert keepout["outline_points"] == 3


def test_list_zones_ignores_footprint_embedded_zones(tmp_path: Path) -> None:
    embedded = BOARD_CODED.replace(
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))',
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))\n'
        '        (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "fp-zone")\n'
        '          (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))',
    )
    board = _board(tmp_path, content=embedded)
    tools = _tools(tmp_path)

    result = json.loads(tools["list_zones"](path=str(board)))
    assert result["zone_count"] == 0


# ── Z3 delete_zone ───────────────────────────────────────────────────────────

def test_delete_zone_by_name(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE)
    tools = _tools(tmp_path)

    result = json.loads(tools["delete_zone"](path=str(board), name="gnd_pour"))

    assert result["status"] == "success"
    assert result["deleted"] == "zone-copper-1"
    assert result["via"] == "file"
    assert "zone-copper-1" not in board.read_text(encoding="utf-8")


def test_delete_zone_selector_validation(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE)
    tools = _tools(tmp_path)

    none_given = json.loads(tools["delete_zone"](path=str(board)))
    assert none_given["error_code"] == "zone_selector_invalid"
    both_given = json.loads(tools["delete_zone"](
        path=str(board), name="gnd_pour", uuid="zone-copper-1"))
    assert both_given["error_code"] == "zone_selector_invalid"
    assert both_given["message"] == "provide exactly one of name or uuid"


def test_delete_zone_ambiguous(tmp_path: Path) -> None:
    twin = COPPER_ZONE.replace("zone-copper-1", "zone-copper-2")
    board = _board(tmp_path, zones=COPPER_ZONE + twin)
    tools = _tools(tmp_path)

    result = json.loads(tools["delete_zone"](path=str(board), name="gnd_pour"))
    assert result["error_code"] == "zone_ambiguous"
    assert "matches 2 zones; use uuid" in result["message"]

    by_uuid = json.loads(tools["delete_zone"](
        path=str(board), uuid="zone-copper-2"))
    assert by_uuid["status"] == "success"
    content = board.read_text(encoding="utf-8")
    assert "zone-copper-1" in content and "zone-copper-2" not in content


def test_delete_zone_refuses_rule_area(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=RULE_AREA_ZONE)
    tools = _tools(tmp_path)

    result = json.loads(tools["delete_zone"](
        path=str(board), name="antenna_keepout"))
    assert result["error_code"] == "zone_is_rule_area"
    assert "use the keep-out tools" in result["message"]
    assert "zone-keepout-1" in board.read_text(encoding="utf-8")


def test_delete_zone_not_found(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE)
    tools = _tools(tmp_path)

    result = json.loads(tools["delete_zone"](path=str(board), name="nope"))
    assert result["error_code"] == "zone_not_found"
    assert result["message"] == "no zone matching nope"


# ── Z5 refill_zones tool ─────────────────────────────────────────────────────

def test_refill_zones_kicad_closed_uses_subprocess(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE)
    tools = _tools(tmp_path)

    with patch.object(zfs, "fill_zones_subprocess",
                      return_value={"filled": True, "zones_filled": 1,
                                    "interpreter": "x"}) as fill:
        result = json.loads(tools["refill_zones"](path=str(board)))

    fill.assert_called_once()
    assert result["status"] == "success"
    assert result["via"] == "file"
    assert result["zones_filled"] == 1


def test_refill_zones_unavailable_when_no_interpreter(tmp_path: Path) -> None:
    board = _board(tmp_path, zones=COPPER_ZONE)
    tools = _tools(tmp_path)

    with patch.object(zfs, "fill_zones_subprocess", return_value=_NO_FILL):
        result = json.loads(tools["refill_zones"](path=str(board)))

    assert result["status"] == "unavailable"
    assert result["error_code"] == "zone_refill_unavailable"
    assert "KiCad closed and no bundled interpreter found" in result["reason"]
    assert "test: fill disabled" in result["reason"]


def test_refill_zones_saferefuse_structured(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.get_zone_refill_ops.side_effect = SafeRefuseError(
        "no live path", capability="ZONE_REFILL",
        remedy="open the board in the PCB editor", paths_tried=["ipc"],
    )
    board = _board(tmp_path)
    tools = _tools(tmp_path, backend=backend)

    result = json.loads(tools["refill_zones"](path=str(board)))
    assert result["status"] == "error"
    assert result["error_code"] == "kicad_running_no_live_path"
    assert result["remedy"] == "open the board in the PCB editor"


def test_refill_zones_zoneless_no_save_file_path(tmp_path: Path) -> None:
    board = _board(tmp_path)  # no zones
    tools = _tools(tmp_path)
    mtime_before = board.stat().st_mtime_ns

    def _boom(*a: Any, **k: Any):
        raise AssertionError("zoneless refill must not invoke the subprocess")

    with patch.object(zfs, "fill_zones_subprocess", side_effect=_boom):
        result = json.loads(tools["refill_zones"](path=str(board)))

    assert result["status"] == "success"
    assert result["zones_filled"] == 0
    assert result["note"] == "board has no zones"
    assert board.stat().st_mtime_ns == mtime_before


# ── Z4 subprocess helper contract ────────────────────────────────────────────

def test_fill_zones_subprocess_loadboard_script() -> None:
    # Regression pin for measured facts 2/3: the generated script LoadBoards
    # the saved file — never a constructed BOARD() — counts zones before
    # filling, and prints the one-line JSON contract.
    assert "LoadBoard" in zfs._FILL_SCRIPT
    assert "BOARD()" not in zfs._FILL_SCRIPT
    assert zfs._FILL_SCRIPT.index("count") < zfs._FILL_SCRIPT.index("ZONE_FILLER")
    assert "if count > 0:" in zfs._FILL_SCRIPT
    assert 'json.dumps({"zones_filled": count, "filled": count > 0})' \
        in zfs._FILL_SCRIPT


def test_fill_zones_subprocess_no_interpreter(tmp_path: Path) -> None:
    with patch.object(zfs, "find_bundled_python", return_value=None):
        result = zfs.fill_zones_subprocess(tmp_path / "b.kicad_pcb")
    assert result == {"filled": False,
                      "reason": "no bundled KiCad python interpreter found"}


def test_fill_zones_subprocess_parses_json_line(tmp_path: Path) -> None:
    proc = MagicMock(returncode=0, stdout='noise\n{"zones_filled": 2, "filled": true}\n',
                     stderr="")
    with patch.object(zfs, "find_bundled_python",
                      return_value=Path("py.exe")), \
         patch("subprocess.run", return_value=proc):
        result = zfs.fill_zones_subprocess(tmp_path / "b.kicad_pcb")
    assert result["filled"] is True
    assert result["zones_filled"] == 2


def test_find_bundled_python_prefers_board_format_major(tmp_path: Path) -> None:
    # Live finding 2026-09-14: filling a KiCad-9 board with the 10.0
    # interpreter silently re-saved it in KiCad-10 format (unopenable in 9).
    # The selector must try the board's own major first.
    for v in ("10.0", "9.0"):
        (tmp_path / v).mkdir()
        (tmp_path / v / "python.exe").write_text("", encoding="utf-8")
    with patch.object(zfs, "probe_order", return_value=["10.0", "9.0"]), \
         patch.object(zfs, "bin_dir", side_effect=lambda v: tmp_path / v):
        assert zfs.find_bundled_python("9") == tmp_path / "9.0" / "python.exe"
        assert zfs.find_bundled_python() == tmp_path / "10.0" / "python.exe"
        # A major with no install falls back to the probe order.
        assert zfs.find_bundled_python("8") == tmp_path / "10.0" / "python.exe"


def _k9_board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
                 '\t(generator_version "9.0")\n)\n', encoding="utf-8")
    return p


def test_fill_zones_subprocess_warns_on_format_upgrade(tmp_path: Path) -> None:
    proc = MagicMock(returncode=0,
                     stdout='{"zones_filled": 1, "filled": true}\n', stderr="")
    with patch.object(zfs, "find_bundled_python",
                      return_value=Path(r"C:\kc\10.0\bin\python.exe")), \
         patch("subprocess.run", return_value=proc):
        result = zfs.fill_zones_subprocess(_k9_board(tmp_path))
    assert result["filled"] is True
    assert "now in KiCad 10 format" in result["warnings"][0]


def test_fill_zones_subprocess_no_warning_when_matched(tmp_path: Path) -> None:
    proc = MagicMock(returncode=0,
                     stdout='{"zones_filled": 1, "filled": true}\n', stderr="")
    with patch.object(zfs, "find_bundled_python",
                      return_value=Path(r"C:\kc\9.0\bin\python.exe")) as finder, \
         patch("subprocess.run", return_value=proc):
        result = zfs.fill_zones_subprocess(_k9_board(tmp_path))
    finder.assert_called_once_with("9")
    assert "warnings" not in result


def test_fill_zones_subprocess_nonzero_exit(tmp_path: Path) -> None:
    proc = MagicMock(returncode=3, stdout="", stderr="0xC0000005")
    with patch.object(zfs, "find_bundled_python",
                      return_value=Path("py.exe")), \
         patch("subprocess.run", return_value=proc):
        result = zfs.fill_zones_subprocess(tmp_path / "b.kicad_pcb")
    assert result["filled"] is False
    assert "exited 3" in result["reason"]


# ── PluginBoardOps TCP serialization ─────────────────────────────────────────

def _patch_tcp(returned: dict | None = None):
    captured: dict = {}

    def fake_tcp_call(method: str, timeout: float, **kwargs: Any):
        captured["method"] = method
        captured["kwargs"] = kwargs
        return returned or {}

    return patch("kicad_mcp.backends.plugin_backend._tcp_call",
                 side_effect=fake_tcp_call), captured


def test_plugin_board_ops_dispatches_add_zone() -> None:
    patcher, captured = _patch_tcp(returned={"status": "success"})
    outline = [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]
    with patcher:
        PluginBoardOps().add_zone(
            Path("/tmp/b.kicad_pcb"), "GND", "B.Cu", outline,
            pad_connection="solid", name="pour", refill=False,
        )
    assert captured["method"] == "add_zone"
    kw = captured["kwargs"]
    assert kw["net"] == "GND"
    assert kw["layer"] == "B.Cu"
    assert kw["outline"] == outline
    assert kw["pad_connection"] == "solid"
    assert kw["name"] == "pour"
    assert kw["refill"] is False


def test_plugin_board_ops_dispatches_delete_zone() -> None:
    patcher, captured = _patch_tcp(returned={"status": "success"})
    with patcher:
        PluginBoardOps().delete_zone(Path("/tmp/b.kicad_pcb"), uuid="abc")
    assert captured["method"] == "delete_zone"
    assert captured["kwargs"]["uuid"] == "abc"
    assert captured["kwargs"]["name"] == ""


# ── bridge dispatch + zoneless no-save (handler with stubs) ──────────────────

@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location(
        "kicad_mcp_bridge_zones_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_registers_zone_handlers(bridge) -> None:
    assert "add_zone" in bridge._DISPATCH
    assert "delete_zone" in bridge._DISPATCH
    assert "add_zone" in bridge._MUTATING_METHODS
    assert "delete_zone" in bridge._MUTATING_METHODS
    assert bridge._BRIDGE_VERSION == "2.8.0-tracemaker"


def test_bridge_refill_zoneless_no_save(bridge, monkeypatch) -> None:
    fake_board = MagicMock()
    fake_board.Zones.return_value = []
    saves: list = []
    monkeypatch.setattr(bridge, "_run_on_main_thread", lambda fn: fn())
    monkeypatch.setattr(bridge, "_get_open_board", lambda path: fake_board)
    monkeypatch.setattr(bridge, "_save_and_refresh",
                        lambda board: saves.append(board))
    monkeypatch.setitem(sys.modules, "pcbnew", MagicMock())

    result = bridge._handle_refill_zones("/tmp/b.kicad_pcb")

    assert result == {"status": "ok", "zones_filled": 0,
                      "note": "board has no zones"}
    assert saves == [], "zoneless refill must not save (Z5 no-dirty rule)"


def test_bridge_refill_with_zones_saves(bridge, monkeypatch) -> None:
    fake_board = MagicMock()
    fake_board.Zones.return_value = [MagicMock()]
    saves: list = []
    monkeypatch.setattr(bridge, "_run_on_main_thread", lambda fn: fn())
    monkeypatch.setattr(bridge, "_get_open_board", lambda path: fake_board)
    monkeypatch.setattr(bridge, "_save_and_refresh",
                        lambda board: saves.append(board))
    monkeypatch.setitem(sys.modules, "pcbnew", MagicMock())

    result = bridge._handle_refill_zones("/tmp/b.kicad_pcb")

    assert result == {"status": "ok", "zones_filled": 1}
    assert saves == [fake_board]
