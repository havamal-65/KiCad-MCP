"""Live oracle for the symbol-pin rotation audit (#17 residual, REQ-SYM-1, V-M).

eeschema never writes a placed pin's absolute position to disk and KiCad 9 has
no eeschema scripting API, so the ground truth is captured through KiCad's own
connectivity engine, headlessly:

    Place a no_connect marker at each position get_symbol_pin_positions computes,
    then run `kicad-cli sch erc`. A marker exactly on eeschema's pin silences
    that pin and is itself "used"; a marker off the pin fires BOTH
    pin_not_connected AND no_connect_dangling.

So zero pin_not_connected + zero no_connect_dangling across all 36 pins proves
every computed pin position coincides with eeschema's pin -- the audit's oracle.
This is the schematic analogue of the board test that confirms footprint
rotation against pcbnew's DRC engine.

Opt-in like the rest of tests/integration (needs kicad-cli); requires no bridge
and no open KiCad -- kicad-cli runs fully headless.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileSchematicOps
from kicad_mcp.utils.platform_helper import find_kicad_cli

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "rotation_audit"
_SCH = _FIXTURE_DIR / "rotation_audit.kicad_sch"


def _run_erc(cli: Path, sch: Path, out: Path) -> dict:
    subprocess.run(
        [str(cli), "sch", "erc", "--format", "json", "--units", "mm",
         "--severity-all", "-o", str(out), str(sch)],
        capture_output=True, text=True, timeout=120,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def _violation_counts(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            counts[v["type"]] = counts.get(v["type"], 0) + 1
    return counts


@pytest.mark.integration
def test_pin_positions_land_on_eeschema_pins(tmp_path: Path) -> None:
    """No-connect round-trip: every computed pin position is exactly on the
    eeschema pin, for all rotation x mirror combinations (REQ-SYM-1)."""
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")

    ops = FileSchematicOps()
    work = tmp_path / "rotation_audit.kicad_sch"
    work.write_text(_SCH.read_text(encoding="utf-8"), encoding="utf-8")

    ground_truth = json.loads(
        (_FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8")
    )

    placed = 0
    for ref in ground_truth:
        pins = ops.get_symbol_pin_positions(work, ref)["pin_positions"]
        for pos in pins.values():
            ops.add_no_connect(work, pos["x"], pos["y"])
            placed += 1
    assert placed == 36, f"expected 36 pins, placed {placed}"

    counts = _violation_counts(_run_erc(cli, work, tmp_path / "erc.json"))
    assert counts.get("pin_not_connected", 0) == 0, (
        f"a no-connect missed its pin -> computed position is wrong: {counts}"
    )
    assert counts.get("no_connect_dangling", 0) == 0, (
        f"a no-connect landed off any pin -> computed position is wrong: {counts}"
    )


@pytest.mark.integration
def test_round_trip_detects_a_deliberately_wrong_position(tmp_path: Path) -> None:
    """Sanity check on the oracle itself: nudging one pin off by a grid step
    must make ERC fire, so a clean result in the test above is meaningful."""
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")

    ops = FileSchematicOps()
    work = tmp_path / "rotation_audit.kicad_sch"
    work.write_text(_SCH.read_text(encoding="utf-8"), encoding="utf-8")

    # Place correct no-connects for every pin except one, which we shift 2.54 mm.
    first = True
    for ref in json.loads(
        (_FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8")
    ):
        pins = ops.get_symbol_pin_positions(work, ref)["pin_positions"]
        for pos in pins.values():
            if first:
                ops.add_no_connect(work, pos["x"] + 2.54, pos["y"])
                first = False
            else:
                ops.add_no_connect(work, pos["x"], pos["y"])

    counts = _violation_counts(_run_erc(cli, work, tmp_path / "erc.json"))
    assert (
        counts.get("pin_not_connected", 0) > 0
        or counts.get("no_connect_dangling", 0) > 0
    ), f"oracle failed to flag a deliberately misplaced no-connect: {counts}"
