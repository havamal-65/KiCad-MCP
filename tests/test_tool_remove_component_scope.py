"""Tests for the remove_component tool's scope param (#3 tool surface) and
the PluginBoardOps remove_component TCP op."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import fastmcp
import pytest

from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps
from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
)
from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog


_SCH = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 100 47 0))
        (property "Value" "10k" (at 100 53 0))
      )
    )
""")

_PCB = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "VCC")
      (footprint "Resistor_SMD:R_0603_1608Metric"
        (layer "F.Cu")
        (at 50 60)
        (uuid "aaaaaaaa-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 -1.5 0)
          (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (property "Value" "10k" (at 0 1.5 0)
          (layer "F.Fab")
          (effects (font (size 1 1) (thickness 0.15)))
        )
        (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu")
          (net 1 "VCC")
        )
      )
    )
""")


class _FileBackend:
    def get_schematic_modify_ops(self):
        return FileSchematicOps()

    def get_board_modify_ops(self):
        return FileBoardOps()


class _BridgeDownBoardOps:
    def remove_component(self, path, reference):
        raise BridgeTemporarilyUnavailableError("bridge down")


class _BridgeDownBackend(_FileBackend):
    def get_board_modify_ops(self):
        return _BridgeDownBoardOps()


def _get_tool(backend, tmp_path: Path):
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "remove_component"
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "proj.kicad_sch").write_text(_SCH, encoding="utf-8")
    (tmp_path / "proj.kicad_pcb").write_text(_PCB, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Tool scope routing
# ---------------------------------------------------------------------------

def test_default_scope_schematic_back_compat(project: Path):
    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_sch"), "R1"))

    assert result["status"] == "success"
    assert result["removed"] is True
    assert '"R1"' not in (project / "proj.kicad_sch").read_text(encoding="utf-8")
    # PCB untouched
    assert '"R1"' in (project / "proj.kicad_pcb").read_text(encoding="utf-8")


def test_scope_pcb_removes_footprint_and_returns_state(project: Path):
    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_pcb"), "R1", scope="pcb"))

    assert result["status"] == "success"
    assert result["removed"] is True
    assert result["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert result["position"] == {"x": 50.0, "y": 60.0}
    assert result["pad_nets"] == {"1": "VCC"}
    assert '"R1"' not in (project / "proj.kicad_pcb").read_text(encoding="utf-8")
    # Schematic untouched
    assert '"R1"' in (project / "proj.kicad_sch").read_text(encoding="utf-8")


def test_scope_pcb_falls_back_to_file_when_bridge_down(project: Path):
    tool = _get_tool(_BridgeDownBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_pcb"), "R1", scope="pcb"))

    assert result["status"] == "success"
    assert result["removed"] is True
    assert '"R1"' not in (project / "proj.kicad_pcb").read_text(encoding="utf-8")


def test_scope_both_removes_from_both_files(project: Path):
    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_sch"), "R1", scope="both"))

    assert result["status"] == "success"
    assert result["schematic"]["removed"] is True
    assert result["pcb"]["removed"] is True
    assert result["pcb"]["pad_nets"] == {"1": "VCC"}
    assert '"R1"' not in (project / "proj.kicad_sch").read_text(encoding="utf-8")
    assert '"R1"' not in (project / "proj.kicad_pcb").read_text(encoding="utf-8")


def test_scope_both_partial_when_missing_on_one_side(project: Path):
    """Ref present only in the schematic → partial status, per-side detail."""
    pcb = project / "proj.kicad_pcb"
    pcb_no_r1 = _PCB.replace('"R1"', '"R9"')
    pcb.write_text(pcb_no_r1, encoding="utf-8")

    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_sch"), "R1", scope="both"))

    assert result["status"] == "partial"
    assert result["schematic"]["removed"] is True
    assert "error" in result["pcb"]


def test_invalid_scope_rejected(project: Path):
    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_sch"), "R1", scope="board"))

    assert result["status"] == "error"
    assert "scope" in result["message"]


def test_scope_pcb_not_found_is_error(project: Path):
    tool = _get_tool(_FileBackend(), project)
    result = json.loads(tool(str(project / "proj.kicad_pcb"), "U99", scope="pcb"))

    assert result["status"] == "error"
    assert "U99" in result["message"]


# ---------------------------------------------------------------------------
# PluginBoardOps TCP op
# ---------------------------------------------------------------------------

def test_plugin_board_ops_remove_component_tcp_call(tmp_path: Path):
    payload = {
        "reference": "R1", "removed": True,
        "footprint": "Lib:FP",
        "position": {"x": 50.0, "y": 60.0}, "rotation": 0.0,
        "layer": "F.Cu", "locked": False, "pad_nets": {"1": "VCC"},
    }
    with patch(
        "kicad_mcp.backends.plugin_backend._tcp_call", return_value=payload
    ) as tcp:
        result = PluginBoardOps().remove_component(tmp_path / "b.kicad_pcb", "R1")

    assert result == payload
    method, _timeout = tcp.call_args[0]
    assert method == "remove_component"
    assert tcp.call_args[1]["reference"] == "R1"
    assert tcp.call_args[1]["path"].endswith("b.kicad_pcb")
