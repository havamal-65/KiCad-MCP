"""Tests for §6.4 differential-pair rules (REQ-TEST-001…007).

`set_board_design_rules` gains optional `differential_pairs` (named netclasses
with diff-pair width/gap + net assignment in `.kicad_pro`) and `length_matching`
(length constraints in the sibling `.kicad_dru`). Most tests drive the impl
(`FileBoardOps`) directly; validation lives in the MCP tool layer, so the
validation test pulls the tool fn from a registered FastMCP instance.

The ground-truth fixture is a verbatim copy of KiCad 9's shipped `vme-wren`
demo (REQ-FMT-001) — see tests/fixtures/projects/README.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.tools import board as board_mod
from kicad_mcp.utils.change_log import ChangeLog

FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "diffpair_ground_truth.kicad_pro"

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


def _project(tmp_path: Path) -> Path:
    """Write a minimal .kicad_pcb + sibling .kicad_pro; return the .kicad_pcb path."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps(_MIN_PRO, indent=2) + "\n", encoding="utf-8")
    return pcb


def _read_pro(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))


def _class(pro: dict, name: str) -> dict | None:
    return next((c for c in pro["net_settings"]["classes"] if c.get("name") == name), None)


def _tool(tmp_path: Path):
    mcp = fastmcp.FastMCP("test")
    board_mod.register_tools(mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "set_board_design_rules"
    )


_USB = {"name": "USB", "nets": ["USB_D+", "USB_D-"], "width_mm": 0.20, "gap_mm": 0.13}


# ── REQ-TEST-001 — USB-C pair (AC6 core) ─────────────────────────────────────

def test_usbc_pair_creates_netclass_and_assigns_nets(tmp_path):
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(pcb, "class2", differential_pairs=[dict(_USB)])

    pro = _read_pro(pcb)
    usb = _class(pro, "USB")
    assert usb is not None
    assert usb["diff_pair_width"] == 0.20
    assert usb["diff_pair_gap"] == 0.13
    assert usb["diff_pair_via_gap"] == 0.13  # defaulted from gap_mm
    assigned = {
        p["pattern"] for p in pro["net_settings"]["netclass_patterns"]
        if p["netclass"] == "USB"
    }
    assert assigned == {"USB_D+", "USB_D-"}
    assert _class(pro, "Default") is not None  # Default preserved


def test_via_gap_override_is_honoured(tmp_path):
    pcb = _project(tmp_path)
    dp = dict(_USB, via_gap_mm=0.25)
    FileBoardOps().set_board_design_rules(pcb, "class2", differential_pairs=[dp])
    assert _class(_read_pro(pcb), "USB")["diff_pair_via_gap"] == 0.25


# ── REQ-TEST-002 — idempotency ───────────────────────────────────────────────

def test_idempotent_rerun_leaves_single_class(tmp_path):
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(pcb, "class2", differential_pairs=[dict(_USB)])
    FileBoardOps().set_board_design_rules(pcb, "class2", differential_pairs=[dict(_USB)])

    pro = _read_pro(pcb)
    usb_classes = [c for c in pro["net_settings"]["classes"] if c["name"] == "USB"]
    assert len(usb_classes) == 1
    pat = [
        p for p in pro["net_settings"]["netclass_patterns"]
        if p["pattern"] in ("USB_D+", "USB_D-")
    ]
    assert len(pat) == 2  # no duplicate assignment entries


# ── REQ-TEST-003 — multi-pair distinct geometry ──────────────────────────────

def test_multi_pair_distinct_geometry(tmp_path):
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(
        pcb, "class2",
        differential_pairs=[
            dict(_USB),
            {"name": "ETH", "nets": ["MDI0+", "MDI0-"], "width_mm": 0.25, "gap_mm": 0.15},
        ],
    )
    pro = _read_pro(pcb)
    usb, eth = _class(pro, "USB"), _class(pro, "ETH")
    assert (usb["diff_pair_width"], usb["diff_pair_gap"]) == (0.20, 0.13)
    assert (eth["diff_pair_width"], eth["diff_pair_gap"]) == (0.25, 0.15)
    assert usb["priority"] != eth["priority"]
    assert usb["priority"] != 2147483647 and eth["priority"] != 2147483647


def test_net_pattern_assignment(tmp_path):
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(
        pcb, "class2",
        differential_pairs=[{"name": "HDMI", "net_pattern": "HDMI_D*",
                             "width_mm": 0.20, "gap_mm": 0.13}],
    )
    pats = _read_pro(pcb)["net_settings"]["netclass_patterns"]
    assert {"netclass": "HDMI", "pattern": "HDMI_D*"} in pats


# ── REQ-TEST-004 — validation rejects bad input, writes nothing ───────────────

def test_validation_rejects_bad_input_and_writes_nothing(tmp_path):
    pcb = _project(tmp_path)
    pro_path = pcb.with_suffix(".kicad_pro")
    before = pro_path.read_bytes()
    tool = _tool(tmp_path)

    one_net = tool(str(pcb), "class2",
                   differential_pairs=[{"name": "USB", "nets": ["only_one"],
                                        "width_mm": 0.20, "gap_mm": 0.13}])
    assert json.loads(one_net)["status"] == "error"

    neg_width = tool(str(pcb), "class2",
                     differential_pairs=[{"name": "USB", "nets": ["a", "b"],
                                          "width_mm": -0.1, "gap_mm": 0.13}])
    assert json.loads(neg_width)["status"] == "error"

    dup = tool(str(pcb), "class2",
               differential_pairs=[dict(_USB), dict(_USB)])
    assert json.loads(dup)["status"] == "error"

    assert pro_path.read_bytes() == before  # nothing written on any error
    assert not pcb.with_suffix(".kicad_dru").exists()


# ── REQ-TEST-005 — backward compat: preset-only unchanged ────────────────────

def test_preset_only_unaffected_by_additive_params(tmp_path):
    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(pcb, "class2")
    pro = _read_pro(pcb)
    assert [c["name"] for c in pro["net_settings"]["classes"]] == ["Default"]
    assert pro["net_settings"]["netclass_patterns"] == []
    assert "differential_pairs_applied" not in {}  # sanity
    assert not pcb.with_suffix(".kicad_dru").exists()


def test_preset_only_return_has_no_diffpair_keys(tmp_path):
    pcb = _project(tmp_path)
    res = FileBoardOps().set_board_design_rules(pcb, "class2")
    assert "differential_pairs_applied" not in res
    assert "length_matching_applied" not in res


# ── REQ-TEST-006 — structural match against the real KiCad fixture ───────────

def test_structural_match_against_ground_truth(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    gt_named = next(
        c for c in fixture["net_settings"]["classes"] if c["name"] != "Default"
    )
    gt_pattern = fixture["net_settings"]["netclass_patterns"][0]

    pcb = _project(tmp_path)
    FileBoardOps().set_board_design_rules(pcb, "class2", differential_pairs=[dict(_USB)])
    pro = _read_pro(pcb)
    usb = _class(pro, "USB")

    # Every key the real KiCad-authored named netclass carries is present on ours
    # → KiCad 9 loads it without "repair".
    assert set(gt_named).issubset(set(usb)), set(gt_named) - set(usb)
    # Assignment entry shape matches the fixture exactly ({"netclass", "pattern"}).
    our_pattern = pro["net_settings"]["netclass_patterns"][0]
    assert set(our_pattern) == set(gt_pattern)


# ── REQ-TEST-007 — length matching writes a valid .kicad_dru ──────────────────

def test_length_matching_writes_dru_and_keeps_widths(tmp_path):
    pcb = _project(tmp_path)
    res = FileBoardOps().set_board_design_rules(
        pcb, "class2",
        differential_pairs=[dict(_USB)],
        length_matching=[{"group": "USB", "target_mm": 50.0, "tolerance_mm": 0.5}],
    )
    dru = pcb.with_suffix(".kicad_dru")
    assert dru.exists()
    text = dru.read_text(encoding="utf-8")
    assert "(version 1)" in text
    assert 'rule "length_USB"' in text
    assert "(constraint length" in text
    assert "(min 49.5mm)" in text
    assert "(max 50.5mm)" in text
    assert "(opt 50mm)" in text
    assert "A.NetClass == 'USB'" in text

    # diff-pair widths are still applied (AC6 met even with length matching)
    assert _class(_read_pro(pcb), "USB")["diff_pair_width"] == 0.20
    assert res["length_matching_applied"][0]["group"] == "USB"
    assert res["dru_path"].endswith(".kicad_dru")


def test_length_matching_preserves_existing_dru_rules(tmp_path):
    """The balanced-paren upsert keeps unrelated rules (incl. quoted parens)."""
    pcb = _project(tmp_path)
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(
        '(version 1)\n\n'
        '(rule "keepme"\n'
        "\t(constraint length (min 10mm) (max 11mm))\n"
        "\t(condition \"A.NetClass == 'X' && A.fromTo('U1-*','U2-*')\")\n"
        ")\n",
        encoding="utf-8",
    )
    FileBoardOps().set_board_design_rules(
        pcb, "class2",
        length_matching=[{"group": "USB", "target_mm": 50.0, "tolerance_mm": 0.5}],
    )
    text = dru.read_text(encoding="utf-8")
    assert 'rule "keepme"' in text         # unrelated rule preserved
    assert "A.fromTo('U1-*','U2-*')" in text  # quoted parens intact
    assert 'rule "length_USB"' in text     # new rule added
    assert text.count("(version 1)") == 1  # header not duplicated


def test_length_matching_rule_is_idempotent(tmp_path):
    pcb = _project(tmp_path)
    lm = [{"group": "USB", "target_mm": 50.0, "tolerance_mm": 0.5}]
    FileBoardOps().set_board_design_rules(pcb, "class2", length_matching=lm)
    FileBoardOps().set_board_design_rules(pcb, "class2", length_matching=lm)
    text = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert text.count('rule "length_USB"') == 1
