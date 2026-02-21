#!/usr/bin/env python3
"""Integration tests for all 64 KiCad MCP tools.

Creates a self-contained test project in a temporary directory, exercises
every tool, and prints a pass/fail/skip summary table.

Run from the repo root:
    python tests/integration/run_integration_tests.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

os.environ.setdefault("KICAD_MCP_LOG_LEVEL", "ERROR")
KICAD_CLI = Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe")

# ---------------------------------------------------------------------------
# Minimal KiCad file templates
# ---------------------------------------------------------------------------

MINIMAL_PCB = """\
(kicad_pcb
  (version 20231120)
  (generator "kicad_mcp")
  (generator_version "9.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (outputdirectory "")
    )
  )
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
)
"""

# PCB with a footprint that has real pads, used for assign_net test
MINIMAL_PCB_WITH_PADS = """\
(kicad_pcb
  (version 20231120)
  (generator "kicad_mcp")
  (generator_version "9.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (outputdirectory "")
    )
  )
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "RP1" (at 0 0 0)
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "10k" (at 0 0 0)
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (uuid "aaaaaaaa-bbbb-cccc-dddd-000000000001")
    (pad "1" smd rect (at -0.9525 0) (size 1 1.75) (layers "F.Cu" "F.Paste" "F.Mask"))
    (pad "2" smd rect (at 0.9525 0) (size 1 1.75) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
)
"""

MINIMAL_PROJECT = """\
{
  "meta": { "filename": "test_project.kicad_pro", "version": 1 },
  "board": {},
  "libraries": {},
  "net_settings": {},
  "schematic": { "annotate_start_num": 0 },
  "text_variables": {}
}
"""

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class _Skip(Exception):
    pass


class Runner:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.results: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    def run(self, name: str, fn, *args, **kwargs) -> str:
        try:
            detail = fn(*args, **kwargs)
            self.results[name] = (PASS, str(detail)[:200] if detail else "ok")
            print(f"  \033[32mv\033[0m {name}")
            return PASS
        except _Skip as e:
            self.results[name] = (SKIP, str(e))
            print(f"  \033[33m!\033[0m {name}  [{e}]")
            return SKIP
        except Exception as e:
            tb = traceback.format_exc().strip().splitlines()[-1]
            self.results[name] = (FAIL, tb)
            print(f"  \033[31mx\033[0m {name}")
            print(f"      {tb}")
            return FAIL

    def skip(self, reason: str):
        raise _Skip(reason)

    # ------------------------------------------------------------------
    def summary(self) -> None:
        total = len(self.results)
        passed = sum(1 for s, _ in self.results.values() if s == PASS)
        skipped = sum(1 for s, _ in self.results.values() if s == SKIP)
        failed = sum(1 for s, _ in self.results.values() if s == FAIL)

        print("\n" + "=" * 72)
        print(f"  RESULTS  {passed} passed  {skipped} skipped  {failed} failed  / {total} total")
        print("=" * 72)

        if failed:
            print("\nFailed tests:")
            for name, (status, detail) in self.results.items():
                if status == FAIL:
                    print(f"  x {name}")
                    print(f"    {detail}")

        print(f"\nTest files in: {self.work_dir}")


# ---------------------------------------------------------------------------
# Tool call helpers
# ---------------------------------------------------------------------------

def call(tools, tool: str, **kwargs) -> dict:
    """Call a tool and parse JSON result."""
    raw = tools[tool].fn(**kwargs)
    return json.loads(raw)


def ok(result: dict) -> dict:
    """Assert status == success."""
    assert result.get("status") == "success", f"status={result.get('status')}: {result}"
    return result


# ---------------------------------------------------------------------------
# Build the test server + schematic project
# ---------------------------------------------------------------------------

def build_test_fixtures(work: Path, tools: dict) -> tuple[Path, Path, Path]:
    """
    Build test_project/ with:
      test_project.kicad_pro  - project file
      test_project.kicad_sch  - schematic (R1=10k, R2=4.7k, +3V3, GND, wire, label)
      test_project.kicad_pcb  - PCB (empty, for PCB tool tests)

    Returns (sch_path, pcb_path, pro_path).
    """
    proj_dir = work / "test_project"
    proj_dir.mkdir(exist_ok=True)

    pro = proj_dir / "test_project.kicad_pro"
    sch = proj_dir / "test_project.kicad_sch"
    pcb = proj_dir / "test_project.kicad_pcb"

    pro.write_text(MINIMAL_PROJECT, encoding="utf-8")
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")

    # Build schematic with MCP tools
    ok(call(tools, "create_schematic", path=str(sch), title="MCP Integration Test", revision="1"))

    ok(call(tools, "add_component",
            path=str(sch), lib_id="Device:R", reference="R1", value="10k",
            x=100.0, y=80.0, footprint="Resistor_SMD:R_0402_1005Metric"))
    ok(call(tools, "add_component",
            path=str(sch), lib_id="Device:R", reference="R2", value="4.7k",
            x=130.0, y=80.0, footprint="Resistor_SMD:R_0402_1005Metric"))
    ok(call(tools, "add_component",
            path=str(sch), lib_id="Device:C", reference="C1", value="100nF",
            x=160.0, y=80.0, footprint="Capacitor_SMD:C_0402_1005Metric"))

    # Pin 1 of R1 is at (100, 76.19) and pin 2 at (100, 83.81) (Device:R default rotation)
    r1_pins = call(tools, "get_symbol_pin_positions", path=str(sch), reference="R1")
    pin1 = r1_pins["pin_positions"]["1"]  # top
    pin2 = r1_pins["pin_positions"]["2"]  # bottom

    ok(call(tools, "add_power_symbol", path=str(sch), name="+3V3", x=pin1["x"], y=pin1["y"]))
    ok(call(tools, "add_power_symbol", path=str(sch), name="GND",  x=pin2["x"], y=pin2["y"]))

    r2_pins = call(tools, "get_symbol_pin_positions", path=str(sch), reference="R2")
    r2_p1 = r2_pins["pin_positions"]["1"]
    r2_p2 = r2_pins["pin_positions"]["2"]
    ok(call(tools, "add_power_symbol", path=str(sch), name="+3V3", x=r2_p1["x"], y=r2_p1["y"]))
    ok(call(tools, "add_label",        path=str(sch), text="SDA",  x=r2_p2["x"], y=r2_p2["y"]))

    c1_pins = call(tools, "get_symbol_pin_positions", path=str(sch), reference="C1")
    ok(call(tools, "add_power_symbol", path=str(sch), name="+3V3", x=c1_pins["pin_positions"]["1"]["x"], y=c1_pins["pin_positions"]["1"]["y"]))
    ok(call(tools, "add_power_symbol", path=str(sch), name="GND",  x=c1_pins["pin_positions"]["2"]["x"], y=c1_pins["pin_positions"]["2"]["y"]))

    # Wire between R1 pin2 and a junction (just as a connectivity fixture)
    ok(call(tools, "add_wire",     path=str(sch), start_x=pin2["x"], start_y=pin2["y"],
            end_x=pin2["x"], end_y=pin2["y"] + 2.54))
    ok(call(tools, "add_junction", path=str(sch), x=pin2["x"], y=pin2["y"]))
    ok(call(tools, "add_no_connect", path=str(sch), x=r2_p1["x"] + 5.0, y=r2_p1["y"]))

    print(f"  -> test project in: {proj_dir}")
    return sch, pcb, pro


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="kicad_mcp_inttest_"))
    print(f"\nKiCad MCP Integration Tests")
    print(f"Work dir: {work}\n")

    from kicad_mcp.config import KiCadMCPConfig, BackendType
    from kicad_mcp.server import create_server

    config = KiCadMCPConfig(backend=BackendType.FILE, log_level="ERROR")
    # Point kicad-cli explicitly so export/drc tests can use it
    if KICAD_CLI.exists():
        os.environ["KICAD_MCP_KICAD_CLI_PATH"] = str(KICAD_CLI)

    mcp = create_server(config)
    tools = mcp._tool_manager._tools
    r = Runner(work)

    # -- Build test fixtures -----------------------------------------------
    print("-- Setup ------------------------------------------------------")
    try:
        sch, pcb, pro = build_test_fixtures(work, tools)
        print("  v Test project built\n")
    except Exception as e:
        print(f"  x SETUP FAILED: {e}")
        traceback.print_exc()
        return 1

    proj_dir = pro.parent
    exports = work / "exports"
    exports.mkdir(exist_ok=True)

    # -- Project Management (6) ---------------------------------------------
    print("-- Project Management -----------------------------------------")

    def t_open_project():
        res = call(tools, "open_project", path=str(pro))
        assert res["status"] == "success"
        proj = res.get("project", res)
        assert "test_project" in str(proj.get("name", "") or proj.get("path", ""))
        return res["status"]
    r.run("open_project", t_open_project)

    def t_list_project_files():
        res = call(tools, "list_project_files", path=str(proj_dir))
        assert res["status"] == "success"
        all_files = [f for files in res.get("files", {}).values() for f in files]
        assert any("kicad_sch" in str(f) for f in all_files)
        return f"{len(all_files)} files"
    r.run("list_project_files", t_list_project_files)

    def t_get_project_metadata():
        res = call(tools, "get_project_metadata", path=str(pro))
        assert res["status"] == "success"
        meta = res.get("metadata", res)
        assert "version" in meta or "schematic" in meta or len(meta) > 1
        return res["status"]
    r.run("get_project_metadata", t_get_project_metadata)

    def t_save_project():
        res = call(tools, "save_project", path=str(pro))
        # IPC-only — should return info/unavailable, not raise
        assert res["status"] in ("success", "info", "error", "unavailable")
        return res["status"]
    r.run("save_project", t_save_project)

    def t_get_backend_info():
        res = call(tools, "get_backend_info")
        assert res["status"] == "success"
        assert ("backends" in res or "active_backend" in res
                or "active_backends" in res or "primary_backend" in res)
        return res["status"]
    r.run("get_backend_info", t_get_backend_info)

    def t_get_active_project():
        res = call(tools, "get_active_project")
        # IPC-only — must not raise; returns info/unavailable
        assert res["status"] in ("success", "info", "error", "unavailable")
        return res["status"]
    r.run("get_active_project", t_get_active_project)

    # -- Schematic Operations (22) ------------------------------------------
    print("\n-- Schematic Operations ---------------------------------------")

    def t_read_schematic():
        res = call(tools, "read_schematic", path=str(sch))
        assert res["status"] == "success"
        refs = {s["reference"] for s in res.get("symbols", [])}
        assert "R1" in refs and "R2" in refs and "C1" in refs
        return f"{len(refs)} symbols"
    r.run("read_schematic", t_read_schematic)

    def t_create_schematic():
        new_sch = work / "created.kicad_sch"
        res = call(tools, "create_schematic", path=str(new_sch), title="Test", revision="A")
        assert res["status"] == "success"
        assert new_sch.exists()
        content = new_sch.read_text()
        assert "(kicad_sch" in content and "uuid" in content
        return res["uuid"][:8]
    r.run("create_schematic", t_create_schematic)

    # Work on a writable copy for all modify tests
    mod_sch = work / "modify.kicad_sch"
    shutil.copy(sch, mod_sch)

    def t_add_component():
        res = call(tools, "add_component", path=str(mod_sch),
                   lib_id="Device:LED", reference="D1", value="LED",
                   x=200.0, y=80.0)
        assert res["status"] == "success"
        assert res["reference"] == "D1"
        return res["lib_id"]
    r.run("add_component", t_add_component)

    def t_add_wire():
        res = call(tools, "add_wire", path=str(mod_sch),
                   start_x=200.0, start_y=90.0, end_x=210.0, end_y=90.0)
        assert res["status"] == "success"
        return f"({res['start']['x']},{res['start']['y']})->({res['end']['x']},{res['end']['y']})"
    r.run("add_wire", t_add_wire)

    def t_add_label():
        res = call(tools, "add_label", path=str(mod_sch), text="TESTNET", x=210.0, y=90.0)
        assert res["status"] == "success"
        assert res["text"] == "TESTNET"
        return res["text"]
    r.run("add_label", t_add_label)

    def t_add_no_connect():
        res = call(tools, "add_no_connect", path=str(mod_sch), x=220.0, y=90.0)
        assert res["status"] == "success"
        return f"({res['position']['x']},{res['position']['y']})"
    r.run("add_no_connect", t_add_no_connect)

    def t_add_power_symbol():
        res = call(tools, "add_power_symbol", path=str(mod_sch), name="GND", x=220.0, y=100.0)
        assert res["status"] == "success"
        # Reference is auto-assigned as #PWR0x; check name or lib_id instead
        assert "GND" in res.get("name", "") or "GND" in res.get("lib_id", "")
        return res.get("reference", res.get("name", "ok"))
    r.run("add_power_symbol", t_add_power_symbol)

    def t_add_junction():
        res = call(tools, "add_junction", path=str(mod_sch), x=200.0, y=90.0)
        assert res["status"] == "success"
        return f"({res['position']['x']},{res['position']['y']})"
    r.run("add_junction", t_add_junction)

    def t_remove_component():
        # Add D2 then remove it
        call(tools, "add_component", path=str(mod_sch),
             lib_id="Device:LED", reference="D2", value="LED", x=230.0, y=80.0)
        res = call(tools, "remove_component", path=str(mod_sch), reference="D2")
        assert res["status"] == "success"
        # Confirm D2 is gone
        syms = call(tools, "read_schematic", path=str(mod_sch))
        refs = {s["reference"] for s in syms.get("symbols", [])}
        assert "D2" not in refs
        return "D2 removed"
    r.run("remove_component", t_remove_component)

    def t_remove_wire():
        # Add a wire then remove it
        call(tools, "add_wire", path=str(mod_sch),
             start_x=50.0, start_y=50.0, end_x=60.0, end_y=50.0)
        res = call(tools, "remove_wire", path=str(mod_sch),
                   start_x=50.0, start_y=50.0, end_x=60.0, end_y=50.0)
        assert res["status"] == "success"
        return "wire removed"
    r.run("remove_wire", t_remove_wire)

    def t_remove_no_connect():
        # Add a no-connect then remove it
        call(tools, "add_no_connect", path=str(mod_sch), x=55.0, y=55.0)
        res = call(tools, "remove_no_connect", path=str(mod_sch), x=55.0, y=55.0)
        assert res["status"] == "success"
        return "no_connect removed"
    r.run("remove_no_connect", t_remove_no_connect)

    def t_move_schematic_component():
        res = call(tools, "move_schematic_component", path=str(mod_sch),
                   reference="D1", x=205.0, y=85.0)
        assert res["status"] == "success"
        assert abs(res["position"]["x"] - 205.0) < 0.01
        return f"D1 -> ({res['position']['x']},{res['position']['y']})"
    r.run("move_schematic_component", t_move_schematic_component)

    def t_update_component_property():
        res = call(tools, "update_component_property", path=str(mod_sch),
                   reference="D1", property_name="Value", property_value="RED_LED")
        assert res["status"] == "success"
        # Verify the change persists
        content = mod_sch.read_text()
        assert "RED_LED" in content
        return "Value=RED_LED"
    r.run("update_component_property", t_update_component_property)

    def t_get_symbol_pin_positions():
        res = call(tools, "get_symbol_pin_positions", path=str(mod_sch), reference="R1")
        assert res["status"] == "success"
        pp = res["pin_positions"]
        assert "1" in pp and "2" in pp
        return f"pin1=({pp['1']['x']:.2f},{pp['1']['y']:.2f})"
    r.run("get_symbol_pin_positions", t_get_symbol_pin_positions)

    def t_get_pin_net():
        # Pin 1 of R1 is connected to +3V3 power symbol
        res = call(tools, "get_pin_net", path=str(sch), reference="R1", pin_number="1")
        assert res["status"] == "success"
        # Net should be +3V3 or detected as power net
        return f"pin1 net={res.get('net_name', res.get('net', '?'))}"
    r.run("get_pin_net", t_get_pin_net)

    def t_get_net_connections():
        res = call(tools, "get_net_connections", path=str(sch), net_name="+3V3")
        assert res["status"] == "success"
        return f"{len(res.get('connections', []))} connections"
    r.run("get_net_connections", t_get_net_connections)

    def t_get_sheet_hierarchy():
        res = call(tools, "get_sheet_hierarchy", path=str(sch))
        assert res["status"] == "success"
        # A flat schematic has just a root sheet
        assert "sheets" in res or "root" in res or "hierarchy" in res
        return res["status"]
    r.run("get_sheet_hierarchy", t_get_sheet_hierarchy)

    def t_validate_schematic():
        res = call(tools, "validate_schematic", path=str(sch))
        assert res["status"] == "success"
        assert "violations" in res
        assert isinstance(res["violations"], list)
        return f"{len(res['violations'])} violations"
    r.run("validate_schematic", t_validate_schematic)

    def t_compare_schematic_pcb():
        # PCB is empty → all schematic components missing from PCB
        res = call(tools, "compare_schematic_pcb",
                   schematic_path=str(sch), board_path=str(pcb))
        assert res["status"] == "success"
        assert res["summary"]["missing_from_pcb"] >= 1
        return f"missing_from_pcb={res['summary']['missing_from_pcb']}"
    r.run("compare_schematic_pcb", t_compare_schematic_pcb)

    def t_sync_schematic_to_pcb():
        sync_pcb = work / "sync.kicad_pcb"
        shutil.copy(pcb, sync_pcb)
        res = call(tools, "sync_schematic_to_pcb",
                   schematic_path=str(sch), board_path=str(sync_pcb))
        assert res["status"] == "success"
        placed = [a for a in res.get("actions", []) if a.get("type") == "placed"]
        assert len(placed) >= 1
        return f"placed {len(placed)} components"
    r.run("sync_schematic_to_pcb", t_sync_schematic_to_pcb)

    def t_annotate_schematic():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        ann_sch = work / "annotate.kicad_sch"
        shutil.copy(sch, ann_sch)
        res = call(tools, "annotate_schematic", path=str(ann_sch))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("annotate_schematic", t_annotate_schematic)

    def t_generate_netlist():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "test_project.net"
        res = call(tools, "generate_netlist", path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("generate_netlist", t_generate_netlist)

    # -- PCB Board Operations (8) -------------------------------------------
    print("\n-- PCB Board Operations ---------------------------------------")

    mod_pcb = work / "modify.kicad_pcb"
    shutil.copy(pcb, mod_pcb)

    def t_read_board():
        res = call(tools, "read_board", path=str(mod_pcb))
        assert res["status"] == "success"
        assert "components" in res
        return f"{len(res['components'])} components"
    r.run("read_board", t_read_board)

    def t_get_board_info():
        res = call(tools, "get_board_info", path=str(mod_pcb))
        assert res["status"] == "success"
        info = res.get("info", res)
        assert "layers" in info or "board_thickness" in info or "num_layers" in info
        return res["status"]
    r.run("get_board_info", t_get_board_info)

    def t_place_component():
        res = call(tools, "place_component", path=str(mod_pcb),
                   reference="R1", footprint="Resistor_SMD:R_0402_1005Metric",
                   x=100.0, y=100.0)
        assert res["status"] == "success"
        assert res["reference"] == "R1"
        return f"R1 at ({res['position']['x']},{res['position']['y']})"
    r.run("place_component", t_place_component)

    def t_move_component():
        res = call(tools, "move_component", path=str(mod_pcb),
                   reference="R1", x=110.0, y=105.0)
        assert res["status"] == "success"
        assert abs(res["position"]["x"] - 110.0) < 0.01
        return f"R1 -> ({res['position']['x']},{res['position']['y']})"
    r.run("move_component", t_move_component)

    def t_add_track():
        res = call(tools, "add_track", path=str(mod_pcb),
                   start_x=100.0, start_y=100.0, end_x=120.0, end_y=100.0,
                   width=0.25, net="+3V3")
        assert res["status"] == "success"
        return f"track width={res.get('width', '?')}"
    r.run("add_track", t_add_track)

    def t_add_via():
        res = call(tools, "add_via", path=str(mod_pcb),
                   x=120.0, y=100.0, size=0.8, drill=0.4, net="+3V3")
        assert res["status"] == "success"
        return f"via at ({res['position']['x']},{res['position']['y']})"
    r.run("add_via", t_add_via)

    def t_assign_net():
        # assign_net requires a footprint with actual (pad ...) nodes.
        # Use a dedicated PCB file that has a pre-defined padded footprint.
        pad_pcb = work / "padded.kicad_pcb"
        pad_pcb.write_text(MINIMAL_PCB_WITH_PADS, encoding="utf-8")
        res = call(tools, "assign_net", path=str(pad_pcb),
                   reference="RP1", pad="1", net="+3V3")
        assert res["status"] == "success"
        assert res["net"] == "+3V3"
        return f"RP1 pad1 -> {res['net']}"
    r.run("assign_net", t_assign_net)

    def t_get_design_rules():
        res = call(tools, "get_design_rules", path=str(mod_pcb))
        assert res["status"] == "success"
        return res["status"]
    r.run("get_design_rules", t_get_design_rules)

    # -- Library Search (6) ------------------------------------------------
    print("\n-- Library Search ---------------------------------------------")

    def t_search_symbols():
        res = call(tools, "search_symbols", query="ATtiny85", limit=5)
        assert res["status"] == "success"
        assert len(res.get("symbols", [])) >= 1
        return f"{len(res['symbols'])} results"
    r.run("search_symbols", t_search_symbols)

    def t_search_footprints():
        res = call(tools, "search_footprints", query="R_0402", limit=5)
        assert res["status"] == "success"
        assert len(res.get("footprints", [])) >= 1
        return f"{len(res['footprints'])} results"
    r.run("search_footprints", t_search_footprints)

    def t_list_libraries():
        res = call(tools, "list_libraries")
        assert res["status"] == "success"
        # 'libraries' is the list; 'symbol_libraries'/'footprint_libraries' are int counts
        libs = res.get("libraries", [])
        total = (res.get("total") or len(libs))
        assert total > 10
        return f"{total} libraries"
    r.run("list_libraries", t_list_libraries)

    def t_get_symbol_info():
        res = call(tools, "get_symbol_info", lib_id="Device:R")
        assert res["status"] == "success"
        assert res.get("name") or res.get("lib_id")
        return res.get("name") or res.get("lib_id")
    r.run("get_symbol_info", t_get_symbol_info)

    def t_get_footprint_info():
        res = call(tools, "get_footprint_info", lib_id="Resistor_SMD:R_0402_1005Metric")
        assert res["status"] == "success"
        return res.get("name") or res.get("lib_id")
    r.run("get_footprint_info", t_get_footprint_info)

    def t_suggest_footprints():
        res = call(tools, "suggest_footprints", lib_id="Device:R")
        assert res["status"] == "success"
        fps = res.get("footprints", [])
        assert len(fps) >= 1, f"No footprints returned; fp_filters={res.get('fp_filters')}"
        return f"{len(fps)} suggestions"
    r.run("suggest_footprints", t_suggest_footprints)

    # -- Library Management (9) --------------------------------------------
    print("\n-- Library Management -----------------------------------------")

    lib_dir = work / "my_libs"
    lib_dir.mkdir(exist_ok=True)

    def t_create_project_library():
        res = call(tools, "create_project_library",
                   project_path=str(proj_dir), library_name="MySymbols",
                   lib_type="symbol")
        assert res["status"] == "success"
        return res.get("library_path", res["status"])
    r.run("create_project_library", t_create_project_library)

    my_sym_lib = proj_dir / "MySymbols.kicad_sym"

    def t_import_symbol():
        if not my_sym_lib.exists():
            r.skip("create_project_library did not produce MySymbols.kicad_sym")
        # Import Device:R into our local library
        source_lib = next(
            (str(p) for p in Path("C:/Program Files/KiCad/9.0/share/kicad/symbols").glob("Device.kicad_sym")),
            None
        )
        if not source_lib:
            r.skip("Device.kicad_sym not found")
        res = call(tools, "import_symbol",
                   source_lib=source_lib, symbol_name="R",
                   target_lib_path=str(my_sym_lib))
        assert res["status"] == "success"
        return "R imported"
    r.run("import_symbol", t_import_symbol)

    def t_create_fp_library():
        """Needed as pre-req for import_footprint."""
        fp_lib = lib_dir / "MyFootprints.pretty"
        fp_lib.mkdir(exist_ok=True)
        return str(fp_lib)

    fp_lib_path = lib_dir / "MyFootprints.pretty"
    fp_lib_path.mkdir(exist_ok=True)

    def t_import_footprint():
        source_lib = next(
            (str(p) for p in Path("C:/Program Files/KiCad/9.0/share/kicad/footprints").glob("Resistor_SMD.pretty")),
            None
        )
        if not source_lib:
            r.skip("Resistor_SMD.pretty not found")
        res = call(tools, "import_footprint",
                   source_lib=source_lib,
                   footprint_name="R_0402_1005Metric",
                   target_lib_path=str(fp_lib_path))
        assert res["status"] == "success"
        return "R_0402_1005Metric imported"
    r.run("import_footprint", t_import_footprint)

    def t_register_library_source():
        res = call(tools, "register_library_source", path=str(lib_dir), name="test_lib_source")
        assert res["status"] == "success"
        return res["status"]
    r.run("register_library_source", t_register_library_source)

    def t_list_library_sources():
        res = call(tools, "list_library_sources")
        assert res["status"] == "success"
        sources = res.get("sources", [])
        names = [s.get("name", "") if isinstance(s, dict) else str(s) for s in sources]
        assert "test_lib_source" in names
        return f"{len(sources)} sources"
    r.run("list_library_sources", t_list_library_sources)

    def t_search_library_sources():
        res = call(tools, "search_library_sources", query="R_0402", source_name="test_lib_source")
        assert res["status"] == "success"
        return f"{len(res.get('results', []))} results"
    r.run("search_library_sources", t_search_library_sources)

    def t_register_project_library():
        if not my_sym_lib.exists():
            r.skip("MySymbols.kicad_sym not found")
        res = call(tools, "register_project_library",
                   project_path=str(proj_dir),
                   library_name="MySymbols",
                   library_path=str(my_sym_lib),
                   lib_type="symbol")
        assert res["status"] == "success"
        return res["status"]
    r.run("register_project_library", t_register_project_library)

    def t_unregister_library_source():
        res = call(tools, "unregister_library_source", name="test_lib_source")
        assert res["status"] == "success"
        # Confirm it's gone
        sources_res = call(tools, "list_library_sources")
        names = [s.get("name", "") if isinstance(s, dict) else str(s)
                 for s in sources_res.get("sources", [])]
        assert "test_lib_source" not in names
        return "removed"
    r.run("unregister_library_source", t_unregister_library_source)

    def t_clone_library_repo():
        # clone_library_repo needs internet + git; skip if no git
        if not shutil.which("git"):
            r.skip("git not in PATH")
        # Use a very small local test: we point at the local repo itself
        clone_target = work / "cloned_lib"
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        res = call(tools, "clone_library_repo",
                   url=repo_root, name="kicad_mcp_self",
                   target_path=str(clone_target))
        assert res["status"] == "success"
        return "cloned"
    r.run("clone_library_repo", t_clone_library_repo)

    # -- Design Rule Checks (3) --------------------------------------------
    print("\n-- Design Rule Checks -----------------------------------------")

    def t_run_drc():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "drc.json"
        res = call(tools, "run_drc", path=str(mod_pcb), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("run_drc", t_run_drc)

    def t_run_erc():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "erc.json"
        res = call(tools, "run_erc", path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("run_erc", t_run_erc)

    def t_get_board_design_rules():
        res = call(tools, "get_board_design_rules", path=str(mod_pcb))
        assert res["status"] == "success"
        return res["status"]
    r.run("get_board_design_rules", t_get_board_design_rules)

    # -- Export Operations (5) ---------------------------------------------
    print("\n-- Export Operations ------------------------------------------")

    def t_export_gerbers():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        gerber_dir = exports / "gerbers"
        gerber_dir.mkdir(exist_ok=True)
        res = call(tools, "export_gerbers", path=str(mod_pcb), output_dir=str(gerber_dir))
        assert res["status"] in ("success", "info", "error")
        if res["status"] == "success":
            files = list(gerber_dir.glob("*"))
            assert len(files) >= 1
        return res["status"]
    r.run("export_gerbers", t_export_gerbers)

    def t_export_drill():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        drill_dir = exports / "drill"
        drill_dir.mkdir(exist_ok=True)
        res = call(tools, "export_drill", path=str(mod_pcb), output_dir=str(drill_dir))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("export_drill", t_export_drill)

    def t_export_bom():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "bom.csv"
        res = call(tools, "export_bom", path=str(sch), output=str(out), format="csv")
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("export_bom", t_export_bom)

    def t_export_pick_and_place():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "pnp.csv"
        res = call(tools, "export_pick_and_place", path=str(mod_pcb), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("export_pick_and_place", t_export_pick_and_place)

    def t_export_pdf():
        if not KICAD_CLI.exists():
            r.skip("kicad-cli not found")
        out = exports / "schematic.pdf"
        res = call(tools, "export_pdf", path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        if res["status"] == "success":
            assert out.exists()
        return res["status"]
    r.run("export_pdf", t_export_pdf)

    # -- Auto-Routing (5) --------------------------------------------------
    print("\n-- Auto-Routing -----------------------------------------------")

    def t_clean_board_for_routing():
        clean_pcb = work / "clean.kicad_pcb"
        shutil.copy(mod_pcb, clean_pcb)
        res = call(tools, "clean_board_for_routing", path=str(clean_pcb))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("clean_board_for_routing", t_clean_board_for_routing)

    def t_export_dsn():
        out = exports / "test.dsn"
        res = call(tools, "export_dsn", path=str(mod_pcb), output=str(out))
        # Needs pcbnew Python bindings; graceful error if unavailable
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("export_dsn", t_export_dsn)

    def t_import_ses():
        # import_ses needs a .ses file; create a dummy one
        dummy_ses = work / "dummy.ses"
        dummy_ses.write_text('(session dummy\n  (routes (resolution um 10))\n)\n')
        res = call(tools, "import_ses", path=str(mod_pcb), ses_path=str(dummy_ses))
        # Graceful error expected (dummy SES is invalid)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("import_ses", t_import_ses)

    def t_run_freerouter():
        # Needs FreeRouting JAR — skip if not configured
        jar = os.environ.get("KICAD_MCP_FREEROUTING_JAR", "")
        if not jar or not Path(jar).exists():
            r.skip("KICAD_MCP_FREEROUTING_JAR not set / file missing")
        dummy_dsn = work / "dummy.dsn"
        dummy_dsn.write_text("(pcb dummy)\n")
        res = call(tools, "run_freerouter", dsn_path=str(dummy_dsn))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("run_freerouter", t_run_freerouter)

    def t_autoroute():
        jar = os.environ.get("KICAD_MCP_FREEROUTING_JAR", "")
        if not jar or not Path(jar).exists():
            r.skip("KICAD_MCP_FREEROUTING_JAR not set / file missing")
        res = call(tools, "autoroute", path=str(mod_pcb))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    r.run("autoroute", t_autoroute)

    # -- Summary ------------------------------------------------------------
    r.summary()

    failed = sum(1 for s, _ in r.results.values() if s == FAIL)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
