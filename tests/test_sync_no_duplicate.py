"""REQ-DUP-7 (F2/S3, verify-then-fix outcome: VERIFIED COVERED).

``sync_schematic_to_pcb`` reconciles by reference before placing (it only
places refs absent from the PCB read), and any residual duplicate attempt is
stopped by the backend-level placement guard (#16) and surfaced as a
``place_failed`` warning. This regression test pins the covered behavior:
re-syncing an already-synced sheet creates zero duplicates.
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


@pytest.fixture(autouse=True)
def _pass_symbol_footprint_validator():
    """Bypass the §6.2 precondition (tested elsewhere) so the placement
    behavior under test runs."""
    with patch(
        "kicad_mcp.tools.drc.run_validate_symbol_footprint_pairs",
        return_value={
            "passed": True, "checked": 0, "mismatches": [],
            "unresolvable": [], "warnings": [], "over_limit": False,
        },
    ):
        yield


_SCH = textwrap.dedent("""\
    (kicad_sch
      (version 20231120)
      (generator "eeschema")
      (paper "A4")
      (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 100 47 0))
        (property "Value" "10k" (at 100 53 0))
        (property "Footprint" "Test:R_0805" (at 100 50 0))
      )
      (symbol (lib_id "Device:C") (at 120 50 0) (unit 1)
        (uuid "22222222-2222-2222-2222-222222222222")
        (property "Reference" "C1" (at 120 47 0))
        (property "Value" "100nF" (at 120 53 0))
        (property "Footprint" "Test:C_0603" (at 120 50 0))
      )
    )
""")

_EMPTY_PCB = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
    )
""")


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


def _call_sync(sch_path: Path, pcb_path: Path) -> dict:
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(
        mcp, _make_backend(), ChangeLog(pcb_path.parent / "changes.json"))
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "sync_schematic_to_pcb"
    )
    # No .kicad_mod resolution — stub placement path is fine for ref counting.
    with patch("kicad_mcp.backends.file_backend._load_kicad_mod",
               return_value=None):
        return json.loads(tool_fn(str(sch_path), str(pcb_path)))


def _ref_counts(pcb: Path) -> dict[str, int]:
    content = pcb.read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    for m in re.finditer(r'\(property "Reference" "([^"]+)"', content):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def test_first_sync_places_each_ref_once(tmp_path: Path):
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(_SCH, encoding="utf-8")
    pcb.write_text(_EMPTY_PCB, encoding="utf-8")

    result = _call_sync(sch, pcb)
    assert result["status"] == "success"
    placed = [a["reference"] for a in result["actions"] if a["type"] == "placed"]
    assert sorted(placed) == ["C1", "R1"]
    assert _ref_counts(pcb) == {"R1": 1, "C1": 1}


def test_resync_creates_zero_duplicates(tmp_path: Path):
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(_SCH, encoding="utf-8")
    pcb.write_text(_EMPTY_PCB, encoding="utf-8")

    _call_sync(sch, pcb)
    board_after_first = pcb.read_text(encoding="utf-8")

    result = _call_sync(sch, pcb)  # the re-sync — must reconcile, not re-place
    assert result["status"] == "success"
    placed = [a for a in result["actions"] if a["type"] == "placed"]
    assert placed == []
    assert _ref_counts(pcb) == {"R1": 1, "C1": 1}
    # No duplicate blocks appended anywhere.
    assert board_after_first.count("(footprint") == \
        pcb.read_text(encoding="utf-8").count("(footprint")


def test_resync_after_partial_manual_placement(tmp_path: Path):
    """A board where one ref is already placed (e.g. by hand): sync places
    only the missing ref and never doubles the existing one."""
    sch = tmp_path / "proj.kicad_sch"
    pcb = tmp_path / "proj.kicad_pcb"
    sch.write_text(_SCH, encoding="utf-8")
    pcb.write_text(_EMPTY_PCB, encoding="utf-8")

    from kicad_mcp.backends.file_backend import FileBoardOps
    FileBoardOps().place_component(pcb, "R1", "Test:R_0805", 30.0, 30.0)

    result = _call_sync(sch, pcb)
    placed = [a["reference"] for a in result["actions"] if a["type"] == "placed"]
    assert placed == ["C1"]
    assert _ref_counts(pcb) == {"R1": 1, "C1": 1}
