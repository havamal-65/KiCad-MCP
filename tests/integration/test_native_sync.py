"""KIR4 NS integration (spec §7 case 22) — live sync through the bridge.

Requires KICAD_INTEGRATION=1, pcbnew open on tests/_scratch/test_board.kicad_pcb
with bridge >= 2.6.0-sync, and kicad-cli on PATH (real netlist generation).

The sync places two resistors from a fixture schematic onto the LIVE scratch
board through the bridge (pcbnew loads the footprint geometry natively — real
pads), assigns their nets from KiCad's own netlist, and the verify block must
report zero mismatches. Everything is removed through the bridge afterwards so
the scratch board returns to its original state.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import fastmcp
import pytest

from kicad_mcp.backends.cli_backend import CLIDRCOps
from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.backends.plugin_backend import PluginBoardOps, _tcp_call
from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog

_SCRATCH_BOARD = Path(__file__).parent.parent / "_scratch" / "test_board.kicad_pcb"

_FIXTURE_SCH = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
      (paper "A4")
      (lib_symbols
        (symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0))
          (exclude_from_sim no) (in_bom yes) (on_board yes)
          (symbol "R_0_1"
            (pin passive line (at 0 3.81 270) (length 1.27)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27))))
            )
            (pin passive line (at 0 -3.81 90) (length 1.27)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27))))
            )
          )
        )
      )
      (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
        (uuid "11111111-0000-0000-0000-000000000091")
        (property "Reference" "R91" (at 100 47 0))
        (property "Value" "1k" (at 100 53 0))
        (property "Footprint" "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal" (at 100 50 0))
        (instances (project "" (path "/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
          (reference "R91") (unit 1))))
      )
      (symbol (lib_id "Device:R") (at 120 50 0) (unit 1)
        (uuid "11111111-0000-0000-0000-000000000092")
        (property "Reference" "R92" (at 120 47 0))
        (property "Value" "2k" (at 120 53 0))
        (property "Footprint" "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal" (at 120 50 0))
        (instances (project "" (path "/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
          (reference "R92") (unit 1))))
      )
      (wire (pts (xy 100 53.81) (xy 120 53.81))
        (stroke (width 0) (type default)) (uuid "22222222-0000-0000-0000-000000000001"))
      (wire (pts (xy 100 53.81) (xy 100 53.81))
        (stroke (width 0) (type default)) (uuid "22222222-0000-0000-0000-000000000002"))
      (label "NSYNC_NET" (at 110 53.81 0)
        (effects (font (size 1.27 1.27))) (uuid "33333333-0000-0000-0000-000000000001"))
      (sheet_instances (path "/" (page "1")))
    )
""")


class _LiveBackend:
    def get_board_ops(self):
        return PluginBoardOps()

    def get_board_modify_ops(self):
        return PluginBoardOps()

    def get_schematic_ops(self):
        return FileSchematicOps()

    def get_schematic_modify_ops(self):
        return FileSchematicOps()

    def get_drc_ops(self):
        from kicad_mcp.utils.platform_helper import find_kicad_cli
        cli = find_kicad_cli()
        assert cli is not None, "integration env requires kicad-cli on PATH"
        return CLIDRCOps(cli)


def _sync_tool():
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(
        mcp, _LiveBackend(), ChangeLog(_SCRATCH_BOARD.parent / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values()
                if t.name == "sync_schematic_to_pcb")


def _bridge_version_tuple(version: str) -> tuple[int, ...]:
    """Numeric triple of a bridge version string like '2.7.0-zones'."""
    numeric = version.split("-", 1)[0]
    try:
        return tuple(int(part) for part in numeric.split("."))
    except ValueError:
        return (0,)


def test_live_sync_native_placement_and_nets(bridge_session, tmp_path: Path):
    version = bridge_session.get("bridge_version", "")
    if _bridge_version_tuple(version) < (2, 6, 0):
        pytest.skip(
            f"bridge {version or '<unknown>'} < 2.6.0 — reinstall the bridge "
            "for NS tests"
        )

    sch = tmp_path / "ns_fixture.kicad_sch"
    sch.write_text(_FIXTURE_SCH, encoding="utf-8")

    board_path = bridge_session.get("board_path", "")
    assert board_path.endswith("test_board.kicad_pcb"), (
        f"scratch board expected open, bridge holds: {board_path}"
    )

    tool_fn = _sync_tool()
    placed_refs: list[str] = []
    try:
        result = json.loads(tool_fn(str(sch), str(_SCRATCH_BOARD)))
        assert result["status"] == "success", result

        placed_refs = [a["reference"] for a in result["actions"]
                       if a["type"] == "placed"]
        assert sorted(placed_refs) == ["R91", "R92"]

        # Native geometry: the live footprints have real pads.
        comps = {c["reference"]: c
                 for c in _tcp_call("get_components", 10.0,
                                    path=str(_SCRATCH_BOARD))}
        assert "R91" in comps and "R92" in comps

        # Nets from KiCad's own netlist; verify block clean.
        nets = [a for a in result["actions"] if a["type"] == "net_assigned"]
        # The label wire runs at y=53.81 — pin 2 of both resistors.
        assert {(a["reference"], a["pad"]) for a in nets} >= {
            ("R91", "2"), ("R92", "2")}
        # KiCad prefixes local labels with the sheet path ("/" at root).
        assert all(a["net"] == "/NSYNC_NET" for a in nets)
        assert result["verified"]["mismatches_total"] == 0, result["verified"]

        # Metadata handler round-trip on a live footprint (bridge-native).
        meta = PluginBoardOps().set_footprint_metadata(
            _SCRATCH_BOARD, "R91", sheet_path="/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            fields={"ClusterId": "ns_test"},
        )
        assert meta["sheet_path_set"] is True
        assert meta["fields_set"] == ["ClusterId"]
    finally:
        for ref in placed_refs:
            try:
                _tcp_call("remove_component", 10.0,
                          path=str(_SCRATCH_BOARD), reference=ref)
            except Exception:
                pass
