"""REQ-FR-6 (F3/S4, #22) — adaptive via-cost auto-tune on a real board.

Runs the REAL via-cost ladder (each rung a live FreeRouting v2.2.4 run) on a
cleared aqs_v2 DSN and asserts the selector picks a fully-routed result with
fewer vias than FreeRouting's max-completeness default (via_costs=50). The
2026-07-12 live anchor: vc=50→19 vias, vc=100→11, vc=200→3 (0 unrouted) — the
ladder auto-selects vc=200.

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
    assert winner["unrouted"] == 0
    # ...with fewer vias than the via_costs=50 max-completeness default.
    baseline = next((r for r in rungs if r["via_costs"] == 50 and r["unrouted"] == 0), None)
    if baseline is not None:
        assert winner["vias"] < baseline["vias"], (winner, baseline)
    # Auto-tune moved off the default toward a higher via cost.
    assert winner["via_costs"] >= 100
