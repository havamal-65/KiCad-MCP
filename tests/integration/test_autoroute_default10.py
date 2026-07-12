"""REQ-FR-3 (F3/S4, #21) — the default max_passes routes a real board complete.

Measured recipe: on the standard runtime (Java 25 + FreeRouting v2.2.4) the
default ``max_passes=10`` fully routes aqs_v2 — 0 unrouted. This runs the REAL
FreeRouting binary on a cleared aqs_v2 DSN (generated here via pcbnew) and
asserts the honest FR-4 report: status "success", 0 unrouted.

Skips cleanly when KiCad's pcbnew, Java, or the FreeRouting jar is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_AQS = Path(__file__).resolve().parents[2] / "examples" / "air_quality_sensor_v2" / "aqs_v2.kicad_pcb"


def _export_cleared_dsn(board: Path, out_dsn: Path) -> bool:
    """Clear all copper and export a pure routing DSN via KiCad's pcbnew."""
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


def test_autoroute_default_passes_fully_routes(tmp_path: Path):
    from kicad_mcp.config import KiCadMCPConfig
    from kicad_mcp.tools.routing import _impl_run_freerouter
    from kicad_mcp.utils.change_log import ChangeLog
    from kicad_mcp.utils.platform_helper import (
        detect_java_major_version,
        find_freerouting_jar,
        find_java,
    )

    if not _AQS.exists():
        pytest.skip("aqs_v2 example board not present")
    cfg = KiCadMCPConfig(_env_file=None)
    # Resolve Java exactly as _impl_run_freerouter will: config first (honours
    # KICAD_MCP_JAVA_PATH → Java 25), then PATH.
    java = cfg.java_path or find_java()
    if java is None:
        pytest.skip("Java not available")
    jar = find_freerouting_jar(detect_java_major_version(java))
    if jar is None or not jar.exists():
        pytest.skip("FreeRouting jar not available")

    dsn = tmp_path / "aqs_cleared.dsn"
    if not _export_cleared_dsn(_AQS, dsn):
        pytest.skip("pcbnew DSN export unavailable")

    ses = tmp_path / "aqs.ses"
    result = json.loads(_impl_run_freerouter(
        str(dsn), str(ses), 10, "", "",  # default max_passes=10, auto java/jar
        cfg, ChangeLog(tmp_path / "c.json"),
    ))
    # FR-4 honest reporting: success only when nothing is unrouted.
    assert result["status"] == "success", result
    assert result["unrouted"] == 0, result
    assert result["detected_version"] is not None
