"""REQ-FR-6 (F3/S4, #22) + REQ-FR-3 — adaptive via-cost auto-tune on a real board.

Runs the REAL via-cost ladder (each rung a live FreeRouting v2.2.4 run) on a
cleared aqs_v2 DSN and asserts the ladder OUTCOME through the production
selector: the `autoroute` default `max_passes=10`, driven through the
production via-cost ladder, fully routes aqs_v2, and the winner is the
fewest-via fully-routed rung of whatever the router returned.

This test is the suite's REQ-FR-3 anchor as well as the REQ-FR-6 one
(2026-10-05, SW-3): the former ``test_autoroute_default10.py`` pinned the
bare default-cost run — a single FreeRouting pass at the router's built-in
default via cost, a call path the production ``autoroute`` never performs on
v2.x — and was deleted when that heuristic outcome drifted.

Skips cleanly when pcbnew, Java, or a v2.x (via-cost-capable) jar is
unavailable. This is the v2.x path; v1.9.0 has no ladder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_AQS = Path(__file__).resolve().parents[2] / "examples" / "air_quality_sensor_v2" / "aqs_v2.kicad_pcb"


def _export_cleared_dsn(board: Path, out_dsn: Path) -> bool:
    from kicad_mcp.backends.subprocess_backend import _run_pcbnew_script
    script = (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard(r'{board}')\n"
        "for t in list(b.GetTracks()): b.RemoveNative(t)\n"
        f"ok = pcbnew.ExportSpecctraDSN(b, r'{out_dsn}')\n"
        "print('EXPORT_OK' if ok else 'EXPORT_FAIL')\n"
    )
    ok, output = _run_pcbnew_script(script, timeout=120)
    return ok and "EXPORT_OK" in output and out_dsn.exists()


def test_autoroute_autotune_selects_fewest_via_complete(tmp_path: Path):
    from kicad_mcp.config import KiCadMCPConfig
    from kicad_mcp.tools.routing import (
        _VIA_COST_LADDER,
        _impl_run_freerouter,
        _parse_freerouting_track_length,
        _select_via_ladder_winner,
    )
    from kicad_mcp.utils.change_log import ChangeLog
    from kicad_mcp.utils.platform_helper import (
        detect_java_major_version,
        find_freerouting_jar,
        find_java,
    )

    if not _AQS.exists():
        pytest.skip("aqs_v2 example board not present")
    cfg = KiCadMCPConfig(_env_file=None)
    # Resolve Java exactly as the run will (config first → honours Java 25).
    java = cfg.java_path or find_java()
    if java is None:
        pytest.skip("Java not available")
    jar = find_freerouting_jar(detect_java_major_version(java))
    if jar is None or not jar.exists():
        pytest.skip("FreeRouting jar not available")
    if "freerouting-2." not in jar.name:
        pytest.skip("via-cost ladder is v2.x-only; resolved jar is v1.x")

    dsn = tmp_path / "aqs_cleared.dsn"
    if not _export_cleared_dsn(_AQS, dsn):
        pytest.skip("pcbnew DSN export unavailable")

    cl = ChangeLog(tmp_path / "c.json")
    rungs = []
    for vc in _VIA_COST_LADDER:
        ses = tmp_path / f"vc{vc}.ses"
        rj = json.loads(_impl_run_freerouter(
            str(dsn), str(ses), 10, "", "", cfg, cl, via_costs=vc,
        ))
        if ses.exists():
            rungs.append({
                "via_costs": vc,
                "unrouted": rj.get("unrouted"),
                "vias": rj.get("vias"),
                "track_length": _parse_freerouting_track_length(ses),
            })

    assert rungs, "no via-cost rung produced a session"
    winner, complete = _select_via_ladder_winner(rungs)

    # The ladder must land a fully-routed result...
    assert complete is True, rungs
    assert winner["unrouted"] == 0, rungs
    # Ladder order invariant: the rungs were tried in production ladder order
    # (asserts our loop, not the router — drift-proof).
    tried = [r["via_costs"] for r in rungs]
    assert tried == [vc for vc in _VIA_COST_LADDER if vc in tried], tried
    # Selector invariant: the winner is the fewest-via fully-routed rung of
    # whatever the router returned — no pinned per-rung heuristic result.
    complete_rungs = [r for r in rungs if r["unrouted"] == 0]
    assert winner["vias"] == min(r["vias"] for r in complete_rungs), rungs
    assert winner["via_costs"] in _VIA_COST_LADDER, rungs
