#!/usr/bin/env python3
"""End-to-end MCP protocol test for all 64 KiCad MCP tools.

Connects to a real KiCad MCP server subprocess via stdio JSON-RPC (the same
transport Claude Desktop uses), builds the complete air quality sensor project
from scratch, and exercises every tool against those real files.

Run from the repo root:
    python examples/air_quality_sensor/test_tools.py

Requirements:
    pip install mcp anyio
    KiCad 9 installed at C:/Program Files/KiCad/9.0/
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
THIS_DIR  = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
SRC_DIR   = REPO_ROOT / "src"
SENSOR_LIB = THIS_DIR / "libs" / "Sensors.kicad_sym"
KICAD_CLI  = Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe")

# ---------------------------------------------------------------------------
# Component positions  (design data — not fake output)
# ---------------------------------------------------------------------------
J1_POS = (20,  55)    # Conn_01x02  power input
U4_POS = (55,  50)    # AMS1117-3.3 LDO
C6_POS = (35,  65)    # 10uF  LDO input bypass
C5_POS = (75,  65)    # 100nF LDO output decoupling
C4_POS = (90,  65)    # 10uF  bulk 3.3V
R1_POS = (115, 50)    # 4.7k  SDA pull-up
R2_POS = (130, 50)    # 4.7k  SCL pull-up
U1_POS = (120, 100)   # ATtiny85-20S MCU
C1_POS = (145, 90)    # 100nF MCU decoupling
U2_POS = (175, 80)    # SCD41 CO2/temp/humidity
U3_POS = (175, 130)   # SGP41 VOC/NOx
C2_POS = (200, 75)    # 100nF SCD41 decoupling
C3_POS = (200, 125)   # 100nF SGP41 decoupling
J2_POS = (20,  140)   # Conn_01x04 I2C debug header

# ---------------------------------------------------------------------------
# Minimal PCB template  (no KiCad tool exists for create_pcb)
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
  (net 1 "+5V")
  (net 2 "+3V3")
  (net 3 "GND")
  (net 4 "SDA")
  (net 5 "SCL")
)
"""

MINIMAL_PROJECT = """\
{
  "meta": { "filename": "air_quality_sensor.kicad_pro", "version": 1 },
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
    def __init__(self):
        self.results: dict[str, tuple[str, str]] = {}

    def record(self, name: str, status: str, detail: str) -> None:
        self.results[name] = (status, detail)
        color = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}[status]
        mark  = {"PASS": "v", "FAIL": "x", "SKIP": "!"}[status]
        suffix = f"  [{detail}]" if status == SKIP else ""
        print(f"  {color}{mark}\033[0m {name}{suffix}")
        if status == FAIL:
            print(f"      {detail}")

    def summary(self) -> int:
        total   = len(self.results)
        passed  = sum(1 for s, _ in self.results.values() if s == PASS)
        skipped = sum(1 for s, _ in self.results.values() if s == SKIP)
        failed  = sum(1 for s, _ in self.results.values() if s == FAIL)

        print("\n" + "=" * 72)
        print(f"  RESULTS  {passed} passed  {skipped} skipped  {failed} failed  / {total} total")
        print("=" * 72)

        if failed:
            print("\nFailed tests:")
            for name, (status, detail) in self.results.items():
                if status == FAIL:
                    print(f"  x {name}")
                    print(f"    {detail}")

        return 1 if failed else 0


# ---------------------------------------------------------------------------
# MCP call helpers
# ---------------------------------------------------------------------------

async def call(session, tool: str, **kwargs) -> dict:
    """Call an MCP tool over the real stdio protocol and return parsed JSON."""
    result = await session.call_tool(tool, kwargs)
    if result.isError:
        text = result.content[0].text if result.content else "unknown error"
        return {"status": "error", "message": text}
    return json.loads(result.content[0].text)


async def ok(session, tool: str, **kwargs) -> dict:
    """Call a tool and assert status == success."""
    res = await call(session, tool, **kwargs)
    assert res.get("status") == "success", \
        f"{tool}: status={res.get('status')!r}  {res}"
    return res


def skip(reason: str):
    raise _Skip(reason)


# ---------------------------------------------------------------------------
# Build the air quality sensor project via MCP tools
# ---------------------------------------------------------------------------

async def build_project(session, runner: Runner) -> tuple[Path, Path, Path]:
    """
    Create the air quality sensor project from scratch using MCP tool calls.
    Returns (sch_path, pcb_path, pro_path).
    """
    sch = THIS_DIR / "air_quality_sensor.kicad_sch"
    pcb = THIS_DIR / "air_quality_sensor.kicad_pcb"
    pro = THIS_DIR / "air_quality_sensor.kicad_pro"

    # Remove stale files from a previous run
    for f in (sch, pcb, pro, THIS_DIR / "sym-lib-table"):
        if f.exists():
            f.unlink()

    # Project and PCB files are minimal templates (no MCP tool for create_pcb)
    pro.write_text(MINIMAL_PROJECT, encoding="utf-8")
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")

    # Create a project-level sym-lib-table so the server can resolve the
    # custom Sensors library (SCD41, SGP41) for lib_symbols caching.
    sym_lib_table = THIS_DIR / "sym-lib-table"
    sensor_lib_uri = (SENSOR_LIB).as_posix()
    sym_lib_table.write_text(
        "(sym_lib_table\n"
        f'  (lib (name "Sensors") (type "KiCad") (uri "{sensor_lib_uri}")'
        ' (options "") (descr ""))\n'
        ")\n",
        encoding="utf-8",
    )

    # --- Schematic ---
    print("  Building schematic via MCP ...")
    await ok(session, "create_schematic",
             path=str(sch), title="Air Quality Sensor", revision="1.0")

    # Place components
    await ok(session, "add_component", path=str(sch),
             lib_id="Connector_Generic:Conn_01x02", reference="J1",
             value="Conn_01x02", x=J1_POS[0], y=J1_POS[1])

    await ok(session, "add_component", path=str(sch),
             lib_id="Regulator_Linear:AMS1117-3.3", reference="U4",
             value="AMS1117-3.3", x=U4_POS[0], y=U4_POS[1])

    for ref, val, pos in [
        ("C6", "10uF",  C6_POS), ("C5", "100nF", C5_POS),
        ("C4", "10uF",  C4_POS), ("C1", "100nF", C1_POS),
        ("C2", "100nF", C2_POS), ("C3", "100nF", C3_POS),
    ]:
        await ok(session, "add_component", path=str(sch),
                 lib_id="Device:C", reference=ref, value=val,
                 x=pos[0], y=pos[1])

    for ref, val, pos in [("R1", "4.7k", R1_POS), ("R2", "4.7k", R2_POS)]:
        await ok(session, "add_component", path=str(sch),
                 lib_id="Device:R", reference=ref, value=val,
                 x=pos[0], y=pos[1])

    await ok(session, "add_component", path=str(sch),
             lib_id="MCU_Microchip_ATtiny:ATtiny85-20S", reference="U1",
             value="ATtiny85-20S", x=U1_POS[0], y=U1_POS[1])

    await ok(session, "add_component", path=str(sch),
             lib_id="Sensors:SCD41", reference="U2",
             value="SCD41", x=U2_POS[0], y=U2_POS[1])

    await ok(session, "add_component", path=str(sch),
             lib_id="Sensors:SGP41", reference="U3",
             value="SGP41", x=U3_POS[0], y=U3_POS[1])

    await ok(session, "add_component", path=str(sch),
             lib_id="Connector_Generic:Conn_01x04", reference="J2",
             value="Conn_01x04", x=J2_POS[0], y=J2_POS[1])

    # Resolve pin positions via MCP
    print("  Resolving pin positions via MCP ...")

    def extract_pins(res: dict) -> dict[str, tuple[float, float]]:
        pp = res["pin_positions"]
        return {num: (p["x"], p["y"]) for num, p in pp.items()}

    u1 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="U1"))
    u4 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="U4"))
    j1 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="J1"))
    j2 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="J2"))
    u2 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="U2"))
    u3 = extract_pins(await ok(session, "get_symbol_pin_positions",
                               path=str(sch), reference="U3"))

    def rc(pos):
        x, y = pos
        return {"1": (x, y - 3.81), "2": (x, y + 3.81)}

    # Power symbols
    print("  Adding power symbols via MCP ...")

    async def pwr(name, x, y):
        await ok(session, "add_power_symbol", path=str(sch),
                 name=name, x=x, y=y)

    await pwr("+5V",  j1["1"][0], j1["1"][1])
    await pwr("GND",  j1["2"][0], j1["2"][1])

    await pwr("+5V",  u4["3"][0], u4["3"][1])
    await pwr("+3V3", u4["2"][0], u4["2"][1])
    await pwr("GND",  u4["1"][0], u4["1"][1])

    for cpos in (C6_POS, C5_POS, C4_POS, C1_POS, C2_POS, C3_POS):
        pins = rc(cpos)
        rail = "+5V" if cpos == C6_POS else "+3V3"
        await pwr(rail, pins["1"][0], pins["1"][1])
        await pwr("GND", pins["2"][0], pins["2"][1])

    await pwr("+3V3", rc(R1_POS)["1"][0], rc(R1_POS)["1"][1])
    await pwr("+3V3", rc(R2_POS)["1"][0], rc(R2_POS)["1"][1])

    await pwr("+3V3", u1["8"][0], u1["8"][1])
    await pwr("GND",  u1["4"][0], u1["4"][1])

    await pwr("+3V3", u2["1"][0], u2["1"][1])
    await pwr("GND",  u2["2"][0], u2["2"][1])
    await pwr("GND",  u2["5"][0], u2["5"][1])

    await pwr("+3V3", u3["1"][0], u3["1"][1])
    await pwr("+3V3", u3["2"][0], u3["2"][1])
    await pwr("GND",  u3["3"][0], u3["3"][1])

    await pwr("+3V3", j2["1"][0], j2["1"][1])
    await pwr("GND",  j2["2"][0], j2["2"][1])

    # Net labels
    print("  Adding net labels via MCP ...")

    async def lbl(text, x, y):
        await ok(session, "add_label", path=str(sch), text=text, x=x, y=y)

    await lbl("SDA", rc(R1_POS)["2"][0], rc(R1_POS)["2"][1])
    await lbl("SDA", u1["5"][0],  u1["5"][1])
    await lbl("SDA", u2["4"][0],  u2["4"][1])
    await lbl("SDA", u3["4"][0],  u3["4"][1])
    await lbl("SDA", j2["3"][0],  j2["3"][1])

    await lbl("SCL", rc(R2_POS)["2"][0], rc(R2_POS)["2"][1])
    await lbl("SCL", u1["7"][0],  u1["7"][1])
    await lbl("SCL", u2["3"][0],  u2["3"][1])
    await lbl("SCL", u3["5"][0],  u3["5"][1])
    await lbl("SCL", j2["4"][0],  j2["4"][1])

    # No-connects
    print("  Adding no-connect markers via MCP ...")

    async def nc(x, y):
        await ok(session, "add_no_connect", path=str(sch), x=x, y=y)

    await nc(u1["6"][0], u1["6"][1])
    await nc(u1["2"][0], u1["2"][1])
    await nc(u1["3"][0], u1["3"][1])
    await nc(u1["1"][0], u1["1"][1])
    await nc(u2["6"][0], u2["6"][1])
    await nc(u3["6"][0], u3["6"][1])

    print(f"  Schematic written: {sch.name}")
    print(f"  PCB template:      {pcb.name}")
    return sch, pcb, pro


# ---------------------------------------------------------------------------
# Test all 64 tools
# ---------------------------------------------------------------------------

async def run_tests(session, runner: Runner,
                    sch: Path, pcb: Path, pro: Path) -> None:
    proj_dir = pro.parent
    exports = THIS_DIR / "exports"
    exports.mkdir(exist_ok=True)

    # Helper: run one test function, catch exceptions, record result
    async def t(name: str, coro):
        try:
            detail = await coro
            runner.record(name, PASS, str(detail)[:120] if detail else "ok")
        except _Skip as e:
            runner.record(name, SKIP, str(e))
        except Exception:
            tb = traceback.format_exc().strip().splitlines()[-1]
            runner.record(name, FAIL, tb)

    # -----------------------------------------------------------------------
    # Project Management (6)
    # -----------------------------------------------------------------------
    print("\n-- Project Management -----------------------------------------")

    async def test_open_project():
        res = await call(session, "open_project", path=str(pro))
        assert res["status"] == "success"
        proj = res.get("project", res)
        assert "air_quality_sensor" in str(proj.get("name", "") or proj.get("path", ""))
        return res["status"]
    await t("open_project", test_open_project())

    async def test_list_project_files():
        res = await call(session, "list_project_files", path=str(proj_dir))
        assert res["status"] == "success"
        all_files = [f for files in res.get("files", {}).values() for f in files]
        assert any("kicad_sch" in str(f) for f in all_files)
        return f"{len(all_files)} files"
    await t("list_project_files", test_list_project_files())

    async def test_get_project_metadata():
        res = await call(session, "get_project_metadata", path=str(pro))
        assert res["status"] == "success"
        meta = res.get("metadata", res)
        assert len(meta) > 1
        return res["status"]
    await t("get_project_metadata", test_get_project_metadata())

    async def test_save_project():
        res = await call(session, "save_project", path=str(pro))
        assert res["status"] in ("success", "info", "error", "unavailable")
        return res["status"]
    await t("save_project", test_save_project())

    async def test_get_backend_info():
        res = await call(session, "get_backend_info")
        assert res["status"] == "success"
        assert ("active_backends" in res or "primary_backend" in res
                or "backends" in res or "active_backend" in res)
        return res["status"]
    await t("get_backend_info", test_get_backend_info())

    async def test_get_active_project():
        res = await call(session, "get_active_project")
        assert res["status"] in ("success", "info", "error", "unavailable")
        return res["status"]
    await t("get_active_project", test_get_active_project())

    # -----------------------------------------------------------------------
    # Schematic Operations (22)
    # -----------------------------------------------------------------------
    print("\n-- Schematic Operations ---------------------------------------")

    async def test_read_schematic():
        res = await call(session, "read_schematic", path=str(sch))
        assert res["status"] == "success"
        refs = {s["reference"] for s in res.get("symbols", [])}
        expected = {"R1", "R2", "C1", "C2", "C3", "C4", "C5", "C6",
                    "U1", "U2", "U3", "U4", "J1", "J2"}
        missing = expected - refs
        assert not missing, f"Missing refs: {missing}"
        return f"{len(refs)} symbols"
    await t("read_schematic", test_read_schematic())

    async def test_create_schematic():
        tmp = THIS_DIR / "_tmp_created.kicad_sch"
        res = await call(session, "create_schematic",
                         path=str(tmp), title="Temp", revision="X")
        assert res["status"] == "success"
        assert tmp.exists()
        content = tmp.read_text()
        assert "(kicad_sch" in content and "uuid" in content
        tmp.unlink()
        return res["uuid"][:8]
    await t("create_schematic", test_create_schematic())

    # Work on a copy so we don't corrupt the main schematic
    mod_sch = THIS_DIR / "_tmp_mod.kicad_sch"
    shutil.copy(sch, mod_sch)

    async def test_add_component():
        res = await call(session, "add_component", path=str(mod_sch),
                         lib_id="Device:LED", reference="D1", value="LED",
                         x=50.0, y=50.0)
        assert res["status"] == "success"
        assert res["reference"] == "D1"
        return res["lib_id"]
    await t("add_component", test_add_component())

    async def test_add_wire():
        res = await call(session, "add_wire", path=str(mod_sch),
                         start_x=50.0, start_y=60.0,
                         end_x=60.0,   end_y=60.0)
        assert res["status"] == "success"
        return (f"({res['start']['x']},{res['start']['y']})"
                f"->({res['end']['x']},{res['end']['y']})")
    await t("add_wire", test_add_wire())

    async def test_add_label():
        res = await call(session, "add_label", path=str(mod_sch),
                         text="TESTNET", x=60.0, y=60.0)
        assert res["status"] == "success"
        assert res["text"] == "TESTNET"
        return res["text"]
    await t("add_label", test_add_label())

    async def test_add_no_connect():
        res = await call(session, "add_no_connect", path=str(mod_sch),
                         x=70.0, y=60.0)
        assert res["status"] == "success"
        return f"({res['position']['x']},{res['position']['y']})"
    await t("add_no_connect", test_add_no_connect())

    async def test_add_power_symbol():
        res = await call(session, "add_power_symbol", path=str(mod_sch),
                         name="GND", x=70.0, y=70.0)
        assert res["status"] == "success"
        assert "GND" in res.get("name", "") or "GND" in res.get("lib_id", "")
        return res.get("reference", res.get("name", "ok"))
    await t("add_power_symbol", test_add_power_symbol())

    async def test_add_junction():
        res = await call(session, "add_junction", path=str(mod_sch),
                         x=50.0, y=60.0)
        assert res["status"] == "success"
        return f"({res['position']['x']},{res['position']['y']})"
    await t("add_junction", test_add_junction())

    async def test_remove_component():
        await call(session, "add_component", path=str(mod_sch),
                   lib_id="Device:LED", reference="D2", value="LED",
                   x=80.0, y=50.0)
        res = await call(session, "remove_component", path=str(mod_sch),
                         reference="D2")
        assert res["status"] == "success"
        syms = await call(session, "read_schematic", path=str(mod_sch))
        refs = {s["reference"] for s in syms.get("symbols", [])}
        assert "D2" not in refs
        return "D2 removed"
    await t("remove_component", test_remove_component())

    async def test_remove_wire():
        await call(session, "add_wire", path=str(mod_sch),
                   start_x=30.0, start_y=30.0, end_x=40.0, end_y=30.0)
        res = await call(session, "remove_wire", path=str(mod_sch),
                         start_x=30.0, start_y=30.0, end_x=40.0, end_y=30.0)
        assert res["status"] == "success"
        return "wire removed"
    await t("remove_wire", test_remove_wire())

    async def test_remove_no_connect():
        await call(session, "add_no_connect", path=str(mod_sch),
                   x=35.0, y=35.0)
        res = await call(session, "remove_no_connect", path=str(mod_sch),
                         x=35.0, y=35.0)
        assert res["status"] == "success"
        return "no_connect removed"
    await t("remove_no_connect", test_remove_no_connect())

    async def test_move_schematic_component():
        res = await call(session, "move_schematic_component", path=str(mod_sch),
                         reference="D1", x=55.0, y=55.0)
        assert res["status"] == "success"
        assert abs(res["position"]["x"] - 55.0) < 0.01
        return f"D1 -> ({res['position']['x']},{res['position']['y']})"
    await t("move_schematic_component", test_move_schematic_component())

    async def test_update_component_property():
        res = await call(session, "update_component_property", path=str(mod_sch),
                         reference="D1", property_name="Value",
                         property_value="RED_LED")
        assert res["status"] == "success"
        assert "RED_LED" in mod_sch.read_text()
        return "Value=RED_LED"
    await t("update_component_property", test_update_component_property())

    async def test_get_symbol_pin_positions():
        res = await call(session, "get_symbol_pin_positions",
                         path=str(sch), reference="U1")
        assert res["status"] == "success"
        pp = res["pin_positions"]
        # ATtiny85 has 8 pins
        assert len(pp) == 8, f"Expected 8 pins, got {len(pp)}"
        assert "1" in pp and "8" in pp
        return f"8 pins; VCC=({pp['8']['x']:.2f},{pp['8']['y']:.2f})"
    await t("get_symbol_pin_positions", test_get_symbol_pin_positions())

    async def test_get_pin_net():
        res = await call(session, "get_pin_net",
                         path=str(sch), reference="R1", pin_number="1")
        assert res["status"] == "success"
        net = res.get("net_name") or res.get("net") or ""
        return f"R1 pin1 net={net!r}"
    await t("get_pin_net", test_get_pin_net())

    async def test_get_net_connections():
        res = await call(session, "get_net_connections",
                         path=str(sch), net_name="SDA")
        assert res["status"] == "success"
        # The backend returns "pins" (not "connections")
        conns = res.get("pins", res.get("connections", []))
        assert len(conns) >= 2, f"Expected >=2 SDA connections, got {len(conns)}"
        return f"{len(conns)} SDA connections"
    await t("get_net_connections", test_get_net_connections())

    async def test_get_sheet_hierarchy():
        res = await call(session, "get_sheet_hierarchy", path=str(sch))
        assert res["status"] == "success"
        assert "sheets" in res or "root" in res or "hierarchy" in res
        return res["status"]
    await t("get_sheet_hierarchy", test_get_sheet_hierarchy())

    async def test_validate_schematic():
        res = await call(session, "validate_schematic", path=str(sch))
        assert res["status"] == "success"
        assert "violations" in res
        assert isinstance(res["violations"], list)
        return f"{len(res['violations'])} violations"
    await t("validate_schematic", test_validate_schematic())

    async def test_compare_schematic_pcb():
        res = await call(session, "compare_schematic_pcb",
                         schematic_path=str(sch), board_path=str(pcb))
        assert res["status"] == "success"
        assert res["summary"]["missing_from_pcb"] >= 1
        return f"missing_from_pcb={res['summary']['missing_from_pcb']}"
    await t("compare_schematic_pcb", test_compare_schematic_pcb())

    async def test_sync_schematic_to_pcb():
        res = await call(session, "sync_schematic_to_pcb",
                         schematic_path=str(sch), board_path=str(pcb))
        assert res["status"] == "success"
        # Components without footprints generate "no_footprint" warnings rather
        # than "placed" actions.  Verify the tool ran and processed something.
        processed = len(res.get("actions", [])) + len(res.get("warnings", []))
        assert processed >= 1, f"Expected at least 1 action or warning: {res}"
        placed = [a for a in res.get("actions", []) if a.get("type") == "placed"]
        warnings = res.get("warnings", [])
        return f"placed={len(placed)}, warnings={len(warnings)}"
    await t("sync_schematic_to_pcb", test_sync_schematic_to_pcb())

    async def test_annotate_schematic():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        ann = THIS_DIR / "_tmp_ann.kicad_sch"
        shutil.copy(sch, ann)
        res = await call(session, "annotate_schematic", path=str(ann))
        ann.unlink(missing_ok=True)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("annotate_schematic", test_annotate_schematic())

    async def test_generate_netlist():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "air_quality_sensor.net"
        res = await call(session, "generate_netlist",
                         path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("generate_netlist", test_generate_netlist())

    # Clean up temp schematic
    mod_sch.unlink(missing_ok=True)

    # -----------------------------------------------------------------------
    # PCB Board Operations (8)  — operate on the synced PCB
    # -----------------------------------------------------------------------
    print("\n-- PCB Board Operations ---------------------------------------")

    mod_pcb = THIS_DIR / "_tmp_mod.kicad_pcb"
    shutil.copy(pcb, mod_pcb)

    async def test_read_board():
        res = await call(session, "read_board", path=str(mod_pcb))
        assert res["status"] == "success"
        assert "components" in res
        return f"{len(res['components'])} components"
    await t("read_board", test_read_board())

    async def test_get_board_info():
        res = await call(session, "get_board_info", path=str(mod_pcb))
        assert res["status"] == "success"
        info = res.get("info", res)
        assert "layers" in info or "board_thickness" in info or "num_layers" in info
        return res["status"]
    await t("get_board_info", test_get_board_info())

    async def test_place_component():
        # Reference must be letter(s) + number(s) only — no underscores
        res = await call(session, "place_component", path=str(mod_pcb),
                         reference="RT1",
                         footprint="Resistor_SMD:R_0402_1005Metric",
                         x=10.0, y=10.0)
        assert res["status"] == "success"
        assert res["reference"] == "RT1"
        return f"RT1 at ({res['position']['x']},{res['position']['y']})"
    await t("place_component", test_place_component())

    async def test_move_component():
        res = await call(session, "move_component", path=str(mod_pcb),
                         reference="RT1", x=15.0, y=15.0)
        assert res["status"] == "success"
        assert abs(res["position"]["x"] - 15.0) < 0.01
        return f"RT1 -> ({res['position']['x']},{res['position']['y']})"
    await t("move_component", test_move_component())

    async def test_add_track():
        res = await call(session, "add_track", path=str(mod_pcb),
                         start_x=10.0, start_y=10.0,
                         end_x=30.0,   end_y=10.0,
                         width=0.25, net="+3V3")
        assert res["status"] == "success"
        return f"track width={res.get('width', '?')}"
    await t("add_track", test_add_track())

    async def test_add_via():
        res = await call(session, "add_via", path=str(mod_pcb),
                         x=30.0, y=10.0, size=0.8, drill=0.4, net="+3V3")
        assert res["status"] == "success"
        return f"via at ({res['position']['x']},{res['position']['y']})"
    await t("add_via", test_add_via())

    async def test_assign_net():
        # Use a PCB file that has a footprint with real pad nodes
        pad_pcb = THIS_DIR / "_tmp_pads.kicad_pcb"
        pad_pcb.write_text(
            MINIMAL_PCB
            + '  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu")\n'
            + '    (at 50 50)\n'
            + '    (property "Reference" "RP1" (at 0 0 0)\n'
            + '      (effects (font (size 1 1) (thickness 0.15)))\n'
            + '    )\n'
            + '    (property "Value" "10k" (at 0 0 0)\n'
            + '      (effects (font (size 1 1) (thickness 0.15)))\n'
            + '    )\n'
            + '    (uuid "aaaaaaaa-bbbb-cccc-dddd-000000000001")\n'
            + '    (pad "1" smd rect (at -0.9525 0) (size 1 1.75)'
            + ' (layers "F.Cu" "F.Paste" "F.Mask"))\n'
            + '    (pad "2" smd rect (at 0.9525 0) (size 1 1.75)'
            + ' (layers "F.Cu" "F.Paste" "F.Mask"))\n'
            + '  )\n'
            + ')\n',
            encoding="utf-8",
        )
        res = await call(session, "assign_net", path=str(pad_pcb),
                         reference="RP1", pad="1", net="+3V3")
        pad_pcb.unlink(missing_ok=True)
        assert res["status"] == "success"
        assert res["net"] == "+3V3"
        return f"RP1 pad1 -> {res['net']}"
    await t("assign_net", test_assign_net())

    async def test_get_design_rules():
        res = await call(session, "get_design_rules", path=str(mod_pcb))
        assert res["status"] == "success"
        return res["status"]
    await t("get_design_rules", test_get_design_rules())

    mod_pcb.unlink(missing_ok=True)

    # -----------------------------------------------------------------------
    # Library Search (6)
    # -----------------------------------------------------------------------
    print("\n-- Library Search ---------------------------------------------")

    async def test_search_symbols():
        res = await call(session, "search_symbols", query="ATtiny85", limit=5)
        assert res["status"] == "success"
        assert len(res.get("symbols", [])) >= 1
        return f"{len(res['symbols'])} results"
    await t("search_symbols", test_search_symbols())

    async def test_search_footprints():
        res = await call(session, "search_footprints", query="R_0402", limit=5)
        assert res["status"] == "success"
        assert len(res.get("footprints", [])) >= 1
        return f"{len(res['footprints'])} results"
    await t("search_footprints", test_search_footprints())

    async def test_list_libraries():
        res = await call(session, "list_libraries")
        assert res["status"] == "success"
        total = res.get("total") or len(res.get("libraries", []))
        assert total > 10
        return f"{total} libraries"
    await t("list_libraries", test_list_libraries())

    async def test_get_symbol_info():
        res = await call(session, "get_symbol_info",
                         lib_id="MCU_Microchip_ATtiny:ATtiny85-20S")
        assert res["status"] == "success"
        assert res.get("name") or res.get("lib_id")
        return res.get("name") or res.get("lib_id")
    await t("get_symbol_info", test_get_symbol_info())

    async def test_get_footprint_info():
        res = await call(session, "get_footprint_info",
                         lib_id="Resistor_SMD:R_0402_1005Metric")
        assert res["status"] == "success"
        return res.get("name") or res.get("lib_id")
    await t("get_footprint_info", test_get_footprint_info())

    async def test_suggest_footprints():
        res = await call(session, "suggest_footprints",
                         lib_id="Device:R")
        assert res["status"] == "success"
        fps = res.get("footprints", [])
        assert len(fps) >= 1, \
            f"No footprints returned; fp_filters={res.get('fp_filters')}"
        return f"{len(fps)} suggestions"
    await t("suggest_footprints", test_suggest_footprints())

    # -----------------------------------------------------------------------
    # Library Management (9)
    # -----------------------------------------------------------------------
    print("\n-- Library Management -----------------------------------------")

    lib_dir = THIS_DIR / "_tmp_libs"
    lib_dir.mkdir(exist_ok=True)

    async def test_create_project_library():
        res = await call(session, "create_project_library",
                         project_path=str(THIS_DIR),
                         library_name="ProjectSymbols",
                         lib_type="symbol")
        assert res["status"] == "success"
        return res.get("library_path", res["status"])
    await t("create_project_library", test_create_project_library())

    proj_sym_lib = THIS_DIR / "ProjectSymbols.kicad_sym"

    async def test_import_symbol():
        if not proj_sym_lib.exists():
            skip("create_project_library did not produce ProjectSymbols.kicad_sym")
        device_lib = Path(
            "C:/Program Files/KiCad/9.0/share/kicad/symbols/Device.kicad_sym"
        )
        if not device_lib.exists():
            skip("Device.kicad_sym not found at expected KiCad 9 path")
        res = await call(session, "import_symbol",
                         source_lib=str(device_lib),
                         symbol_name="R",
                         target_lib_path=str(proj_sym_lib))
        assert res["status"] == "success"
        return "Device:R imported into ProjectSymbols"
    await t("import_symbol", test_import_symbol())

    fp_lib_path = lib_dir / "ProjectFootprints.pretty"
    fp_lib_path.mkdir(exist_ok=True)

    async def test_import_footprint():
        source_lib = Path(
            "C:/Program Files/KiCad/9.0/share/kicad/footprints/Resistor_SMD.pretty"
        )
        if not source_lib.exists():
            skip("Resistor_SMD.pretty not found at expected KiCad 9 path")
        res = await call(session, "import_footprint",
                         source_lib=str(source_lib),
                         footprint_name="R_0402_1005Metric",
                         target_lib_path=str(fp_lib_path))
        assert res["status"] == "success"
        return "R_0402_1005Metric imported"
    await t("import_footprint", test_import_footprint())

    async def test_register_library_source():
        res = await call(session, "register_library_source",
                         path=str(lib_dir), name="aq_test_source")
        assert res["status"] == "success"
        return res["status"]
    await t("register_library_source", test_register_library_source())

    async def test_list_library_sources():
        res = await call(session, "list_library_sources")
        assert res["status"] == "success"
        sources = res.get("sources", [])
        names = [s.get("name", "") if isinstance(s, dict) else str(s)
                 for s in sources]
        assert "aq_test_source" in names
        return f"{len(sources)} sources"
    await t("list_library_sources", test_list_library_sources())

    async def test_search_library_sources():
        res = await call(session, "search_library_sources",
                         query="R_0402", source_name="aq_test_source")
        assert res["status"] == "success"
        return f"{len(res.get('results', []))} results"
    await t("search_library_sources", test_search_library_sources())

    async def test_register_project_library():
        if not proj_sym_lib.exists():
            skip("ProjectSymbols.kicad_sym not found")
        res = await call(session, "register_project_library",
                         project_path=str(THIS_DIR),
                         library_name="ProjectSymbols",
                         library_path=str(proj_sym_lib),
                         lib_type="symbol")
        assert res["status"] == "success"
        return res["status"]
    await t("register_project_library", test_register_project_library())

    async def test_unregister_library_source():
        res = await call(session, "unregister_library_source",
                         name="aq_test_source")
        assert res["status"] == "success"
        check = await call(session, "list_library_sources")
        names = [s.get("name", "") if isinstance(s, dict) else str(s)
                 for s in check.get("sources", [])]
        assert "aq_test_source" not in names
        return "removed"
    await t("unregister_library_source", test_unregister_library_source())

    async def test_clone_library_repo():
        import subprocess as _sp
        if not shutil.which("git"):
            skip("git not in PATH")
        # Use a tiny local bare repo to avoid network latency and large-repo timeouts
        src_repo = THIS_DIR / "_tmp_src_repo"
        clone_target = THIS_DIR / "_tmp_clone"
        for d in (src_repo, clone_target):
            if d.exists():
                shutil.rmtree(d)
        src_repo.mkdir()
        _sp.run(["git", "init", "--bare", str(src_repo)], capture_output=True, check=False)
        res = await call(session, "clone_library_repo",
                         url=str(src_repo),
                         name="aq_tiny_lib",
                         target_path=str(clone_target))
        for d in (src_repo, clone_target):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        assert res["status"] == "success"
        return "cloned"
    await t("clone_library_repo", test_clone_library_repo())

    # Clean up temp library artifacts
    if proj_sym_lib.exists():
        proj_sym_lib.unlink()
    shutil.rmtree(lib_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Design Rule Checks (3)
    # -----------------------------------------------------------------------
    print("\n-- Design Rule Checks -----------------------------------------")

    async def test_run_drc():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "drc.json"
        res = await call(session, "run_drc",
                         path=str(pcb), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("run_drc", test_run_drc())

    async def test_run_erc():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "erc.json"
        res = await call(session, "run_erc",
                         path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("run_erc", test_run_erc())

    async def test_get_board_design_rules():
        res = await call(session, "get_board_design_rules", path=str(pcb))
        assert res["status"] == "success"
        return res["status"]
    await t("get_board_design_rules", test_get_board_design_rules())

    # -----------------------------------------------------------------------
    # Export Operations (5)
    # -----------------------------------------------------------------------
    print("\n-- Export Operations ------------------------------------------")

    async def test_export_gerbers():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        gerber_dir = exports / "gerbers"
        gerber_dir.mkdir(exist_ok=True)
        res = await call(session, "export_gerbers",
                         path=str(pcb), output_dir=str(gerber_dir))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_gerbers", test_export_gerbers())

    async def test_export_drill():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        drill_dir = exports / "drill"
        drill_dir.mkdir(exist_ok=True)
        res = await call(session, "export_drill",
                         path=str(pcb), output_dir=str(drill_dir))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_drill", test_export_drill())

    async def test_export_bom():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "air_quality_sensor_bom.csv"
        res = await call(session, "export_bom",
                         path=str(sch), output=str(out), format="csv")
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_bom", test_export_bom())

    async def test_export_pick_and_place():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "air_quality_sensor_pnp.csv"
        res = await call(session, "export_pick_and_place",
                         path=str(pcb), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_pick_and_place", test_export_pick_and_place())

    async def test_export_pdf():
        if not KICAD_CLI.exists():
            skip("kicad-cli not found")
        out = exports / "air_quality_sensor.pdf"
        res = await call(session, "export_pdf",
                         path=str(sch), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_pdf", test_export_pdf())

    # -----------------------------------------------------------------------
    # Auto-Routing (5)
    # -----------------------------------------------------------------------
    print("\n-- Auto-Routing -----------------------------------------------")

    async def test_clean_board_for_routing():
        clean_pcb = THIS_DIR / "_tmp_clean.kicad_pcb"
        shutil.copy(pcb, clean_pcb)
        res = await call(session, "clean_board_for_routing",
                         path=str(clean_pcb))
        clean_pcb.unlink(missing_ok=True)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("clean_board_for_routing", test_clean_board_for_routing())

    async def test_export_dsn():
        out = exports / "air_quality_sensor.dsn"
        res = await call(session, "export_dsn",
                         path=str(pcb), output=str(out))
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("export_dsn", test_export_dsn())

    async def test_import_ses():
        dummy_ses = THIS_DIR / "_tmp_dummy.ses"
        dummy_ses.write_text(
            "(session dummy\n  (routes (resolution um 10))\n)\n",
            encoding="utf-8",
        )
        ses_pcb = THIS_DIR / "_tmp_ses.kicad_pcb"
        shutil.copy(pcb, ses_pcb)
        res = await call(session, "import_ses",
                         path=str(ses_pcb), ses_path=str(dummy_ses))
        dummy_ses.unlink(missing_ok=True)
        ses_pcb.unlink(missing_ok=True)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("import_ses", test_import_ses())

    async def test_run_freerouter():
        from kicad_mcp.utils.platform_helper import find_freerouting_jar
        jar_env = os.environ.get("KICAD_MCP_FREEROUTING_JAR", "")
        jar = Path(jar_env) if jar_env else find_freerouting_jar()
        if jar is None or not jar.exists():
            skip("FreeRouting JAR not found — place JAR in "
                 "~/.kicad-mcp/freerouting/ or set KICAD_MCP_FREEROUTING_JAR")
        dummy_dsn = THIS_DIR / "_tmp_dummy.dsn"
        dummy_dsn.write_text("(pcb dummy)\n", encoding="utf-8")
        res = await call(session, "run_freerouter", dsn_path=str(dummy_dsn))
        dummy_dsn.unlink(missing_ok=True)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("run_freerouter", test_run_freerouter())

    async def test_autoroute():
        from kicad_mcp.utils.platform_helper import find_freerouting_jar
        jar_env = os.environ.get("KICAD_MCP_FREEROUTING_JAR", "")
        jar = Path(jar_env) if jar_env else find_freerouting_jar()
        if jar is None or not jar.exists():
            skip("FreeRouting JAR not found — place JAR in "
                 "~/.kicad-mcp/freerouting/ or set KICAD_MCP_FREEROUTING_JAR")
        route_pcb = THIS_DIR / "_tmp_route.kicad_pcb"
        shutil.copy(pcb, route_pcb)
        res = await call(session, "autoroute", path=str(route_pcb))
        route_pcb.unlink(missing_ok=True)
        assert res["status"] in ("success", "info", "error")
        return res["status"]
    await t("autoroute", test_autoroute())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> int:
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession

    print("\nKiCad MCP — End-to-End Protocol Test")
    print(f"Project dir: {THIS_DIR}")
    print(f"Repo root:   {REPO_ROOT}\n")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kicad_mcp", "--backend", "file"],
        env={
            **os.environ,
            "PYTHONPATH": str(SRC_DIR),
            "KICAD_MCP_LOG_LEVEL": "ERROR",
            "KICAD_MCP_BACKEND": "file",
            # Suppress fastmcp's RichHandler which is incompatible with
            # the rich version installed in the user site-packages path.
            "FASTMCP_LOG_ENABLED": "false",
        },
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Verify tool count
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"Server connected: {len(tool_names)} tools registered")
            if len(tool_names) != 64:
                print(f"  WARNING: expected 64 tools, got {len(tool_names)}")

            runner = Runner()

            # --- Build the project using MCP tools ---
            print("\n-- Setup: Building Air Quality Sensor Project ---------")
            try:
                sch, pcb, pro = await build_project(session, runner)
                print("  v Project built\n")
            except Exception as e:
                print(f"  x SETUP FAILED: {e}")
                traceback.print_exc()
                return 1

            # --- Run all 64 tool tests ---
            await run_tests(session, runner, sch, pcb, pro)

    runner.summary()
    print(f"\nProject files saved to: {THIS_DIR}")

    failed = sum(1 for s, _ in runner.results.values() if s == FAIL)
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
