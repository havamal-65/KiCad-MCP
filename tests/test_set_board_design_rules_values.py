"""Tests for explicit netclass / rule values on `set_board_design_rules` (F-DV4).

Spec: Specs/KiCad-MCP/dogfood-validation-build/spec-netclass-values.md.
`netclass_values` writes Default-netclass values directly (explicit wins over
preset, applied before the diff-pair seed capture), `rule_values` writes
board.design_settings.rules minimums, and `preset=""` skips the preset for a
values-only call. The backend escape paths (unknown preset, missing .kicad_pro,
effective via_drill >= via_diameter) now return the structured error shape.

Follows the conventions of test_set_board_design_rules_diffpair.py: impl tests
drive `FileBoardOps` directly; validation/error-shape tests pull the tool fn
from a registered FastMCP instance.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.tools import board as board_mod
from kicad_mcp.utils.change_log import ChangeLog

_MIN_PRO = {
    "board": {"design_settings": {"rules": {}}},
    "meta": {"filename": "b.kicad_pro", "version": 3},
    "net_settings": {
        "classes": [],
        "meta": {"version": 4},
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": [],
    },
}

# ecc83-pp.kicad_pro ground truth (KiCad 10 demo) — spec §Measured foundation 7.
_ECC83_NETCLASS = {
    "clearance_mm": 0.4,
    "track_width_mm": 0.8,
    "via_diameter_mm": 1.2,
    "via_drill_mm": 0.6,
    "microvia_diameter_mm": 0.508,
    "microvia_drill_mm": 0.127,
    "diff_pair_width_mm": 0.2,
    "diff_pair_gap_mm": 0.25,
    "diff_pair_via_gap_mm": 0.25,
}
_ECC83_RULES = {
    "min_track_width_mm": 0.2,
    "min_via_diameter_mm": 0.4,
    "min_copper_edge_clearance_mm": 0.01,
    "min_hole_to_hole_mm": 0.25,
}


def _project(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps(_MIN_PRO, indent=2) + "\n", encoding="utf-8")
    return pcb


def _read_pro(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))


def _class(pro: dict, name: str) -> dict | None:
    return next((c for c in pro["net_settings"]["classes"] if c.get("name") == name), None)


def _rules(pro: dict) -> dict:
    return pro["board"]["design_settings"]["rules"]


def _tool(tmp_path: Path):
    mcp = fastmcp.FastMCP("test")
    board_mod.register_tools(mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "set_board_design_rules"
    )


_REF4 = {
    "clearance_mm": 0.4,
    "track_width_mm": 0.8,
    "via_diameter_mm": 1.2,
    "via_drill_mm": 0.6,
}


def test_netclass_values_overrides_default_class(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = FileBoardOps().set_board_design_rules(pcb, "class2", netclass_values=_REF4)
    cls = _class(_read_pro(pcb), "Default")
    assert cls is not None
    assert cls["clearance"] == 0.4
    assert cls["track_width"] == 0.8
    assert cls["via_diameter"] == 1.2
    assert cls["via_drill"] == 0.6
    # rules minimums remain the preset's
    assert _rules(_read_pro(pcb))["min_track_width"] == 0.25
    assert out["netclass_values_applied"] == {
        "clearance": 0.4, "track_width": 0.8, "via_diameter": 1.2, "via_drill": 0.6,
    }


def test_values_only_no_preset(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    # Pre-existing Default with distinctive non-target fields.
    pro = _read_pro(pcb)
    pro["net_settings"]["classes"].append({
        "name": "Default", "bus_width": 99, "wire_width": 42,
        "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
    })
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")

    out = FileBoardOps().set_board_design_rules(pcb, "", netclass_values=_REF4)
    after = _read_pro(pcb)
    cls = _class(after, "Default")
    assert cls is not None
    assert cls["track_width"] == 0.8
    # untouched fields preserved
    assert cls["bus_width"] == 99
    assert cls["wire_width"] == 42
    # no preset applied anywhere
    assert out["preset"] == ""
    assert "rules_applied" not in out
    assert _rules(after) == {}


def test_diffpair_seed_inherits_override(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(
        pcb, "class2",
        differential_pairs=[{"name": "USB", "nets": ["D+", "D-"],
                             "width_mm": 0.20, "gap_mm": 0.13}],
        netclass_values=_REF4,
    )
    usb = _class(_read_pro(pcb), "USB")
    assert usb is not None
    assert usb["track_width"] == 0.8  # seeded from the overridden Default


def test_rule_values_write_design_settings(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = FileBoardOps().set_board_design_rules(
        pcb, "class2",
        rule_values={"min_copper_edge_clearance_mm": 0.01, "min_via_diameter_mm": 0.4},
    )
    rules = _rules(_read_pro(pcb))
    assert rules["min_copper_edge_clearance"] == 0.01
    assert rules["min_via_diameter"] == 0.4
    # the other preset-written minimums are untouched by rule_values
    assert rules["min_clearance"] == 0.20
    assert rules["min_track_width"] == 0.25
    assert rules["min_hole_clearance"] == 0.22
    assert out["rule_values_applied"] == {
        "min_copper_edge_clearance": 0.01, "min_via_diameter": 0.4,
    }


def test_unknown_key_rejected_nothing_written(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    before = pcb.with_suffix(".kicad_pro").read_bytes()
    out = json.loads(_tool(tmp_path)(
        path=str(pcb), netclass_values={"track_mm": 0.8},
    ))
    assert out["status"] == "error"
    assert "netclass_values: unknown key 'track_mm'" in out["message"]
    assert pcb.with_suffix(".kicad_pro").read_bytes() == before


def test_nonpositive_netclass_value_rejected(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = json.loads(_tool(tmp_path)(
        path=str(pcb), netclass_values={"track_width_mm": 0},
    ))
    assert out["status"] == "error"
    assert out["message"] == "netclass_values: track_width_mm must be > 0"


def test_rule_values_allow_zero(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = json.loads(_tool(tmp_path)(
        path=str(pcb), rule_values={"min_clearance_mm": 0.0},
    ))
    assert out["status"] == "success"
    assert _rules(_read_pro(pcb))["min_clearance"] == 0.0


def test_effective_via_drill_geq_diameter_rejected(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    before = pcb.with_suffix(".kicad_pro").read_bytes()
    out = json.loads(_tool(tmp_path)(
        path=str(pcb), preset="class2", netclass_values={"via_drill_mm": 1.3},
    ))
    assert out["status"] == "error"
    assert "effective via_drill (1.3 mm) must be < via_diameter (0.6 mm)" in out["message"]
    assert pcb.with_suffix(".kicad_pro").read_bytes() == before


def test_unknown_preset_returns_structured_error(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = json.loads(_tool(tmp_path)(path=str(pcb), preset="class9000"))
    assert out["status"] == "error"
    assert "Unknown preset 'class9000'" in out["message"]


def test_missing_pro_returns_structured_error(tmp_path: Path) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    out = json.loads(_tool(tmp_path)(path=str(pcb)))
    assert out["status"] == "error"
    assert "No .kicad_pro file found" in out["message"]


def test_pro_backup_created(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = FileBoardOps().set_board_design_rules(pcb, "class2")
    assert "pro_backup_path" in out
    assert Path(out["pro_backup_path"]).exists()


def test_reference_parity_ecc83(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(
        pcb, "", netclass_values=_ECC83_NETCLASS, rule_values=_ECC83_RULES,
    )
    pro = _read_pro(pcb)
    cls = _class(pro, "Default")
    assert cls is not None
    assert cls["clearance"] == 0.4
    assert cls["track_width"] == 0.8
    assert cls["via_diameter"] == 1.2
    assert cls["via_drill"] == 0.6
    assert cls["microvia_diameter"] == 0.508
    assert cls["microvia_drill"] == 0.127
    assert cls["diff_pair_width"] == 0.2
    assert cls["diff_pair_gap"] == 0.25
    assert cls["diff_pair_via_gap"] == 0.25
    rules = _rules(pro)
    assert rules["min_track_width"] == 0.2
    assert rules["min_via_diameter"] == 0.4
    assert rules["min_copper_edge_clearance"] == 0.01
    assert rules["min_hole_to_hole"] == 0.25


def test_below_minimum_warns_nonblocking(tmp_path: Path) -> None:
    pcb = _project(tmp_path)
    out = json.loads(_tool(tmp_path)(
        path=str(pcb), preset="class2", netclass_values={"track_width_mm": 0.1},
    ))
    assert out["status"] == "success"
    assert "track_width 0.1 is below min_track_width 0.25" in out["warnings"]
