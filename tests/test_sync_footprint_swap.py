"""Tests for sync_schematic_to_pcb(apply_footprint_changes=True) (#2).

A footprint mismatch (schematic says R_0805, PCB has R_0603) must be swapped
in place — position, rotation, layer, and pad nets preserved — while
ambiguous cases (locked, unresolvable footprint, incompatible pad names)
are skipped with a reason, never guessed.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import fastmcp
import pytest

from kicad_mcp.tools import schematic
from kicad_mcp.utils.change_log import ChangeLog


_SCH_TEMPLATE = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 100 47 0))
        (property "Value" "10k" (at 100 53 0))
        (property "Footprint" "{footprint}" (at 100 50 0))
      )
    )
""")

_PCB_TEMPLATE = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "VCC")
      (net 2 "GND")
      (footprint "Test:R_0603"
        (layer "F.Cu")
        (at 50 60 90){locked}
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
        (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu")
          (net 2 "GND")
        )
      )
    )
""")

_R_0805_MOD = textwrap.dedent("""\
    (footprint "R_0805"
      (version 20231231)
      (generator "pcbnew")
      (layer "F.Cu")
      (property "Reference" "REF**" (at 0 -1.65 0) (layer "F.SilkS"))
      (property "Value" "R_0805" (at 0 1.65 0) (layer "F.Fab"))
      (fp_rect (start -1.7 -1.2) (end 1.7 1.2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))
      (pad "1" smd roundrect (at -0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "2" smd roundrect (at 0.9375 0) (size 1.025 1.4) (layers "F.Cu" "F.Paste" "F.Mask"))
    )
""")

# Same geometry but alphabetic pad names — incompatible with the old 1/2 pads.
_R_ALPHA_PADS_MOD = _R_0805_MOD.replace('(pad "1"', '(pad "A"').replace('(pad "2"', '(pad "B"')

# Only pad 1 — pad 2's net has nowhere to go (partial match).
_R_ONE_PAD_MOD = "\n".join(
    ln for ln in _R_0805_MOD.splitlines() if '(pad "2"' not in ln
) + "\n"


def _make_backend():
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


def _call_sync(sch_path: Path, pcb_path: Path, mods: dict[str, str], **kwargs) -> dict:
    """Drive sync_schematic_to_pcb with _load_kicad_mod patched to serve *mods*."""
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, _make_backend(), ChangeLog(pcb_path.parent / "changes.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "sync_schematic_to_pcb"
    )

    def fake_load(lib_id: str, project_dir=None):
        return mods.get(lib_id)

    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", side_effect=fake_load):
        return json.loads(tool_fn(str(sch_path), str(pcb_path), **kwargs))


@pytest.fixture()
def project(tmp_path: Path):
    def _write(sch_footprint="Test:R_0805", locked=False):
        sch = tmp_path / "proj.kicad_sch"
        pcb = tmp_path / "proj.kicad_pcb"
        sch.write_text(_SCH_TEMPLATE.format(footprint=sch_footprint), encoding="utf-8")
        pcb.write_text(
            _PCB_TEMPLATE.format(locked="\n    (locked yes)" if locked else ""),
            encoding="utf-8",
        )
        return sch, pcb
    return _write


def _footprint_block(content: str, lib_id: str) -> str:
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
    idx = content.find(f'(footprint "{lib_id}"')
    assert idx != -1, f"{lib_id} not in board file"
    end = _walk_balanced_parens(content, idx)
    assert end is not None
    return content[idx:end + 1]


# ---------------------------------------------------------------------------
# Default flag — warning only, no change (back-compat)
# ---------------------------------------------------------------------------

def test_default_reports_mismatch_without_swapping(project):
    sch, pcb = project()
    result = _call_sync(sch, pcb, {"Test:R_0805": _R_0805_MOD})

    assert result["status"] == "success"
    assert result["summary"]["footprint_changes_applied"] == 0
    mismatches = [w for w in result["warnings"] if w.get("type") == "footprint_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["reference"] == "R1"
    assert "Test:R_0603" in pcb.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# apply_footprint_changes=True — the swap
# ---------------------------------------------------------------------------

def test_swap_preserves_position_rotation_and_nets(project):
    sch, pcb = project()
    result = _call_sync(
        sch, pcb, {"Test:R_0805": _R_0805_MOD}, apply_footprint_changes=True,
    )

    assert result["status"] == "success"
    assert result["summary"]["footprint_changes_applied"] == 1
    assert result["summary"]["footprint_changes_skipped"] == 0

    record = result["footprint_changes_applied"][0]
    assert record["reference"] == "R1"
    assert record["old_footprint"] == "Test:R_0603"
    assert record["new_footprint"] == "Test:R_0805"
    assert record["position"] == {"x": 50.0, "y": 60.0}
    assert record["rotation"] == 90.0
    assert record["nets_reassigned"] == 2
    assert record["unmatched_pads"] == []

    content = pcb.read_text(encoding="utf-8")
    assert "Test:R_0603" not in content
    block = _footprint_block(content, "Test:R_0805")
    # Position + rotation preserved on the new footprint
    at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)", block)
    assert at is not None
    assert (float(at.group(1)), float(at.group(2)), float(at.group(3))) == (50.0, 60.0, 90.0)
    # Nets re-assigned by pad name
    assert re.search(r'\(pad\s+"1"[^()]*(?:\([^()]*\)[^()]*)*\(net\s+1\s+"VCC"\)', block)
    assert re.search(r'\(pad\s+"2"[^()]*(?:\([^()]*\)[^()]*)*\(net\s+2\s+"GND"\)', block)
    # Value rewritten from the .kicad_mod's own text to the schematic value
    assert '(property "Value" "10k"' in block


def test_swap_reports_unmatched_pads(project):
    """New footprint lacks pad 2 — swap applies, pad 2's net is reported."""
    sch, pcb = project()
    result = _call_sync(
        sch, pcb, {"Test:R_0805": _R_ONE_PAD_MOD}, apply_footprint_changes=True,
    )

    assert result["summary"]["footprint_changes_applied"] == 1
    record = result["footprint_changes_applied"][0]
    assert record["nets_reassigned"] == 1
    assert record["unmatched_pads"] == [{"pad": "2", "net": "GND"}]


# ---------------------------------------------------------------------------
# Skip-with-reason cases
# ---------------------------------------------------------------------------

def test_skip_when_new_footprint_unresolvable(project):
    sch, pcb = project(sch_footprint="Test:Missing")
    result = _call_sync(sch, pcb, {}, apply_footprint_changes=True)

    assert result["summary"]["footprint_changes_applied"] == 0
    assert result["summary"]["footprint_changes_skipped"] == 1
    skip = result["footprint_changes_skipped"][0]
    assert skip["reference"] == "R1"
    assert "not found" in skip["reason"]
    # Board untouched
    assert "Test:R_0603" in pcb.read_text(encoding="utf-8")


def test_skip_when_footprint_locked(project):
    sch, pcb = project(locked=True)
    result = _call_sync(
        sch, pcb, {"Test:R_0805": _R_0805_MOD}, apply_footprint_changes=True,
    )

    assert result["summary"]["footprint_changes_applied"] == 0
    skip = result["footprint_changes_skipped"][0]
    assert "locked" in skip["reason"]
    assert "Test:R_0603" in pcb.read_text(encoding="utf-8")


def test_skip_when_pad_names_incompatible(project):
    sch, pcb = project()
    result = _call_sync(
        sch, pcb, {"Test:R_0805": _R_ALPHA_PADS_MOD}, apply_footprint_changes=True,
    )

    assert result["summary"]["footprint_changes_applied"] == 0
    skip = result["footprint_changes_skipped"][0]
    assert "pad names" in skip["reason"]
    assert "Test:R_0603" in pcb.read_text(encoding="utf-8")


def test_skip_when_multi_unit_footprints_conflict(project, tmp_path: Path):
    """Two units of R1 disagreeing on footprint → ambiguous, skipped."""
    sch, pcb = project()
    second_unit = textwrap.dedent("""\
      (symbol (lib_id "Device:R") (at 120 50 0) (unit 2)
        (uuid "22222222-2222-2222-2222-222222222222")
        (property "Reference" "R1" (at 120 47 0))
        (property "Value" "10k" (at 120 53 0))
        (property "Footprint" "Test:R_1206" (at 120 50 0))
      )
    """)
    content = sch.read_text(encoding="utf-8")
    sch.write_text(content.rstrip()[:-1] + second_unit + ")\n", encoding="utf-8")

    result = _call_sync(
        sch, pcb,
        {"Test:R_0805": _R_0805_MOD, "Test:R_1206": _R_0805_MOD},
        apply_footprint_changes=True,
    )

    assert result["summary"]["footprint_changes_applied"] == 0
    assert result["summary"]["footprint_changes_skipped"] == 1
    assert "multi-unit" in result["footprint_changes_skipped"][0]["reason"]
    assert "Test:R_0603" in pcb.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Bridge-path routing (#2 deferred / B1+B3) — swaps go through the live board
# ---------------------------------------------------------------------------

class _RecordingBridgeOps:
    """A stand-in for the live PluginBoardOps — NOT a FileBoardOps, so
    _attempt_footprint_swap treats it as the live bridge and routes the three
    mutations to it instead of editing the file."""

    def __init__(self):
        self.calls: list[tuple] = []

    def remove_component(self, path, reference):
        self.calls.append(("remove_component", reference))
        # Mirror the bridge handler's captured-state payload (S2).
        return {
            "reference": reference,
            "removed": True,
            "footprint": "Test:R_0603",
            "position": {"x": 50.0, "y": 60.0},
            "rotation": 90.0,
            "layer": "F.Cu",
            "locked": False,
            "pad_nets": {"1": "VCC", "2": "GND"},
        }

    def place_component(self, path, reference, footprint, x, y, layer="F.Cu", rotation=0.0):
        self.calls.append(("place_component", reference, footprint, x, y, layer, rotation))
        return {"reference": reference}

    def assign_net(self, path, reference, pad, net):
        self.calls.append(("assign_net", reference, pad, net))
        return {"reference": reference, "pad": pad, "net": net}


def test_swap_routes_mutations_through_live_bridge(project):
    """With a live bridge ops object, the swap's remove/place/assign land on the
    bridge (not the file), and the record is tagged via="bridge"."""
    sch, pcb = project()
    bridge = _RecordingBridgeOps()
    original = pcb.read_text(encoding="utf-8")

    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=_R_0805_MOD):
        swap = schematic._attempt_footprint_swap(
            pcb, "R1", "Test:R_0805", board_ops=bridge,
        )

    assert swap["applied"] is True
    assert swap["via"] == "bridge"
    methods = [c[0] for c in bridge.calls]
    assert methods == ["remove_component", "place_component", "assign_net", "assign_net"]
    # Position/rotation/layer preserved on the bridge place call.
    place = next(c for c in bridge.calls if c[0] == "place_component")
    assert place[3:7] == (50.0, 60.0, "F.Cu", 90.0)
    # The file was NOT edited directly — the bridge owns the mutation.
    assert pcb.read_text(encoding="utf-8") == original


def test_swap_via_file_when_no_bridge(project):
    """Default (board_ops=None) keeps the deterministic file path, via="file"."""
    sch, pcb = project()
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod", return_value=_R_0805_MOD):
        swap = schematic._attempt_footprint_swap(pcb, "R1", "Test:R_0805")

    assert swap["applied"] is True
    assert swap["via"] == "file"
    # File path actually edits the board.
    assert "Test:R_0603" not in pcb.read_text(encoding="utf-8")


def test_no_reload_warning_when_swap_uses_file_backend(project):
    """With the file backend end-to-end (pcb_ops is FileBoardOps), a swap edits
    the file but no live board is open — so no board_reload_required warning."""
    sch, pcb = project()
    result = _call_sync(
        sch, pcb, {"Test:R_0805": _R_0805_MOD}, apply_footprint_changes=True,
    )

    reload_warnings = [w for w in result["warnings"] if w.get("type") == "board_reload_required"]
    assert reload_warnings == []
    # Value still synced to the schematic value through set_footprint_value.
    assert '(property "Value" "10k"' in _footprint_block(
        pcb.read_text(encoding="utf-8"), "Test:R_0805"
    )
