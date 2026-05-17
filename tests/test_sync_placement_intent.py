"""Tests for PlacementIntent → sync_schematic_to_pcb wiring (Phase 6.3.1+6.3.2)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# A minimal JST PH Horizontal .kicad_mod with the "PCB edge" marker on
# Dwgs.User and a courtyard rectangle. Used to mock _load_kicad_mod so
# tests don't depend on a system KiCad install (CI runs without one).
JST_PH_HORIZONTAL_MOD = textwrap.dedent("""\
    (footprint "JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (property "Reference" "REF**" (at 1 -2.55 0) (layer "F.SilkS"))
      (property "Value" "JST_PH" (at 1 7.45 0) (layer "F.Fab"))
      (fp_rect (start -2.36 -1.76) (end 4.36 6.66) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (fp_text user "PCB edge" (at 0 3.65 0) (layer "Dwgs.User"))
      (pad "1" thru_hole oval (at 0 0) (size 1.6 1.6) (drill 1.0) (layers "*.Cu" "*.Mask"))
      (pad "2" thru_hole oval (at 2 0) (size 1.6 1.6) (drill 1.0) (layers "*.Cu" "*.Mask"))
    )
""")


# ---------------------------------------------------------------------------
# Parser-level test (6.3.1): _parse_sch_symbol captures all properties
# ---------------------------------------------------------------------------

def test_parser_captures_placement_intent_property():
    """_parse_sch_symbol must surface non-standard properties under sym['properties']."""
    from kicad_mcp.backends.file_backend import _parse_sch_symbol

    # Build a parsed symbol node (matches what parse_sexp_content produces)
    node = [
        "symbol",
        ["lib_id", "Connector_JST:JST_PH_S2B-PH-K_1x02"],
        ["at", 100.0, 50.0, 0.0],
        ["property", "Reference", "J1"],
        ["property", "Value", "BAT"],
        ["property", "Footprint", "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal"],
        ["property", "PlacementIntent", "edge:south"],
        ["property", "Datasheet", "https://example.com"],
    ]
    sym = _parse_sch_symbol(node)
    assert sym is not None
    # Existing fields still present (back-compat)
    assert sym["reference"] == "J1"
    assert sym["value"] == "BAT"
    assert sym["footprint"] == "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal"
    # New: properties dict captures every property including the special-cased ones
    assert "properties" in sym
    assert sym["properties"]["PlacementIntent"] == "edge:south"
    assert sym["properties"]["Datasheet"] == "https://example.com"
    assert sym["properties"]["Reference"] == "J1"  # also captured here


def test_parser_no_placement_intent_property():
    """Symbols without a PlacementIntent property still parse cleanly."""
    from kicad_mcp.backends.file_backend import _parse_sch_symbol

    node = [
        "symbol",
        ["lib_id", "Device:R"],
        ["property", "Reference", "R1"],
        ["property", "Value", "10k"],
    ]
    sym = _parse_sch_symbol(node)
    assert sym is not None
    assert sym["reference"] == "R1"
    # properties dict is present but doesn't contain PlacementIntent
    assert "PlacementIntent" not in sym.get("properties", {})


# ---------------------------------------------------------------------------
# Sync flow test (6.3.2): PlacementIntent triggers place_at_edge
# ---------------------------------------------------------------------------

# Minimal schematic with a single connector carrying PlacementIntent.
# The S-expression format used by KiCad 9 .kicad_sch files.
_SCH_WITH_INTENT = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (lib_symbols
        (symbol "Connector_JST:JST_PH_S2B-PH-K_1x02"
          (pin_numbers (hide yes))
          (pin_names (offset 1.016))
          (in_bom yes)
          (on_board yes)
        )
      )
      (symbol
        (lib_id "Connector_JST:JST_PH_S2B-PH-K_1x02")
        (at 100 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "J1" (at 100 47 0))
        (property "Value" "BAT" (at 100 53 0))
        (property "Footprint" "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal" (at 100 50 0))
        (property "PlacementIntent" "edge:south" (at 100 55 0))
      )
    )
""")

_PCB_WITH_OUTLINE = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (gr_rect (start 0 0) (end 80 70) (stroke (width 0.05)) (layer "Edge.Cuts"))
    )
""")

_PCB_NO_OUTLINE = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
    )
""")


def _make_backend_with_file_ops():
    """Return a backend whose modify ops are the real FileBoardOps + FileSchematicOps."""
    from kicad_mcp.backends.base import BackendProtocol
    from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps

    class _Backend(BackendProtocol):
        def get_board_ops(self):
            return FileBoardOps()

        def get_board_modify_ops(self):
            return FileBoardOps()

        def get_schematic_ops(self):
            return FileSchematicOps()

        def get_schematic_modify_ops(self):
            return FileSchematicOps()

    return _Backend()


def _call_sync(sch_path: Path, pcb_path: Path) -> dict:
    """Drive sync_schematic_to_pcb with _load_kicad_mod patched to return a
    JST PH Horizontal stub. CI doesn't install KiCad's footprint libraries,
    so without the patch place_component falls back to a stub without silk
    geometry and identify_edge_facing_connectors can't detect the mating face.
    """
    import fastmcp
    from kicad_mcp.tools import schematic
    from kicad_mcp.utils.change_log import ChangeLog

    backend = _make_backend_with_file_ops()
    change_log = ChangeLog(pcb_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, backend, change_log)
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "sync_schematic_to_pcb"
    )
    with patch(
        "kicad_mcp.backends.file_backend._load_kicad_mod",
        return_value=JST_PH_HORIZONTAL_MOD,
    ):
        return json.loads(tool_fn(str(sch_path), str(pcb_path)))


def test_sync_anchors_connector_when_outline_present(tmp_path: Path):
    """edge:south PlacementIntent → connector anchored at south edge after sync."""
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(_SCH_WITH_INTENT, encoding="utf-8")
    pcb.write_text(_PCB_WITH_OUTLINE, encoding="utf-8")

    result = _call_sync(sch, pcb)
    assert result["status"] == "success"

    # An anchored_at_edge action should be present
    edge_actions = [a for a in result["actions"] if a.get("type") == "anchored_at_edge"]
    assert len(edge_actions) == 1, (
        f"Expected one anchored_at_edge action, got actions={result['actions']}"
    )
    assert edge_actions[0]["reference"] == "J1"
    assert edge_actions[0]["edge"] == "south"


def test_sync_defers_when_no_board_outline(tmp_path: Path):
    """No Edge.Cuts → PlacementIntent surfaces as a deferred warning, not a failure."""
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(_SCH_WITH_INTENT, encoding="utf-8")
    pcb.write_text(_PCB_NO_OUTLINE, encoding="utf-8")

    result = _call_sync(sch, pcb)
    assert result["status"] == "success"

    deferred = [w for w in result["warnings"] if w.get("type") == "placement_intent_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["reference"] == "J1"
    assert deferred[0]["intent"] == "edge:south"
    # And the connector is still placed (just at the auto-grid position, not anchored)
    assert any(a.get("type") == "placed" and a["reference"] == "J1" for a in result["actions"])


def test_sync_without_placement_intent_is_back_compat(tmp_path: Path):
    """Symbols without PlacementIntent behave exactly as before."""
    sch_no_intent = _SCH_WITH_INTENT.replace(
        '(property "PlacementIntent" "edge:south" (at 100 55 0))',
        '',
    )
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(sch_no_intent, encoding="utf-8")
    pcb.write_text(_PCB_WITH_OUTLINE, encoding="utf-8")

    result = _call_sync(sch, pcb)
    assert result["status"] == "success"
    # No anchored_at_edge action
    assert not any(a.get("type") == "anchored_at_edge" for a in result["actions"])
    # No placement_intent_deferred warning either
    assert not any(
        w.get("type") == "placement_intent_deferred" for w in result["warnings"]
    )
