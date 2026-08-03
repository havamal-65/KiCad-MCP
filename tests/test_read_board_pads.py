"""F7 — read_board attaches a per-pad net map when 'pads' is requested.

Opt-in only: the pad→net data lets a caller reconstruct a full netlist from one
read_board call. Net names are the reliable key (KiCad-10 boards can be
name-only with no stable net number).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.utils.change_log import ChangeLog

# Two footprints: R1 uses 9-format (net N "NAME"), R2 uses 10-format name-only.
BOARD = """\
(kicad_pcb (version 20241229)
  (footprint "Resistor_SMD:R_0603" (layer "F.Cu")
    (at 10 10 0)
    (property "Reference" "R1" (at 0 0 0))
    (pad "1" smd roundrect (at -0.8 0) (net 3 "SDA"))
    (pad "2" smd roundrect (at 0.8 0) (net 4 "GND"))
  )
  (footprint "Resistor_SMD:R_0603" (layer "F.Cu")
    (at 20 10 0)
    (property "Reference" "R2" (at 0 0 0))
    (pad "1" smd roundrect (at -0.8 0) (net "SDA"))
    (pad "2" smd roundrect (at 0.8 0) (net "GND"))
  )
)
"""


def _tool(tmp_path: Path):
    from kicad_mcp.tools import board as board_mod
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD, encoding="utf-8")
    backend = MagicMock()
    backend.get_board_ops.return_value.read_board.return_value = {
        "info": {"title": "t"},
        "components": [
            {"reference": "R1", "value": "4.7k"},
            {"reference": "R2", "value": "4.7k"},
        ],
        "nets": ["SDA", "GND"],
    }
    mcp = fastmcp.FastMCP("test")
    board_mod.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    fn = next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "read_board")
    return fn, p


def test_read_board_attaches_pad_nets_when_requested(tmp_path: Path) -> None:
    fn, p = _tool(tmp_path)
    res = json.loads(fn(str(p), ["components", "pads"]))
    comps = {c["reference"]: c for c in res["components"]}

    assert {pd["net"] for pd in comps["R1"]["pads"]} == {"SDA", "GND"}
    # KiCad-10 name-only nets resolve too.
    assert {pd["net"] for pd in comps["R2"]["pads"]} == {"SDA", "GND"}


def test_read_board_pads_requires_only_pads_in_include(tmp_path: Path) -> None:
    fn, p = _tool(tmp_path)
    # "pads" alone still implies components (nets attach to them).
    res = json.loads(fn(str(p), ["pads"]))
    assert "components" in res
    assert all("pads" in c for c in res["components"])


def test_read_board_omits_pads_by_default(tmp_path: Path) -> None:
    fn, p = _tool(tmp_path)
    res = json.loads(fn(str(p), ["components"]))
    assert all("pads" not in c for c in res["components"])
    # With no include filter, pads stay opt-in (not attached).
    res_all = json.loads(fn(str(p)))
    assert all("pads" not in c for c in res_all["components"])
