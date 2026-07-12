"""REQ-FR-4 (F3/S4, #22) — FreeRouting result reporting is *excellent*.

`_impl_run_freerouter` must report the detected version, max_passes, elapsed
time, unrouted-connection count and via count, and must NEVER report ``success``
while connections remain unrouted. The unrouted count is parsed from
FreeRouting's own stdout (live-confirmed 2026-07-12 wording); v1.9.0 reports no
count, so completeness is flagged ``unverified`` rather than falsely claimed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_mcp.tools.routing import _impl_run_freerouter
from kicad_mcp.utils.change_log import ChangeLog

# Real v2.2.4 session-completed line captured live (bt_audio DSN, mp=3).
_V2_STDOUT_7_UNROUTED = (
    "2026-07-12 11:10:39 INFO  Freerouting v2.2.4 (build-date: 2026-05-13)\n"
    "2026-07-12 11:10:42 INFO  Auto-router pass #1 ... score of 891.92 (29 unrouted)\n"
    "2026-07-12 11:10:45 INFO  Auto-router pass #3 ... score of 975.19 (7 unrouted)\n"
    "2026-07-12 11:10:45 INFO  Auto-router session completed: started with 111 "
    "unrouted nets, completed in 4.76 seconds, final score: 975.19 (7 unrouted), "
    "using 3.92 total CPU seconds.\n"
)
# Live-confirmed: when fully routed, FreeRouting OMITS the "(N unrouted)"
# parenthetical on the session-completed line (mp=10 on the same board printed
# "(2 unrouted)" at pass #6, then just "score of 995.66" once complete).
_V2_STDOUT_COMPLETE = (
    "INFO  Freerouting v2.2.4 (build-date: 2026-05-13)\n"
    "INFO  Auto-router pass #6 ... with the score of 990.00 (2 unrouted)\n"
    "INFO  Auto-router pass #7 ... with the score of 995.66\n"
    "INFO  Auto-router session completed: started with 111 unrouted nets, "
    "completed in 6.24 seconds, final score: 995.66, using 5.78 CPU seconds.\n"
)
# v1.9.0 headless stdout reports no unrouted count at all (captured live).
_V1_STDOUT = (
    "INFO  Freerouting v1.9.0 (build-date: 2023-10-30)\n"
    "INFO  Auto-routing was completed in 8.33 seconds.\n"
    "INFO  Route optimization was completed in 1.16 seconds.\n"
)


def _cfg(timeout: int = 300):
    return SimpleNamespace(
        java_path=None, freerouting_jar=None,
        freerouting_timeout_seconds=timeout,
    )


def _run(tmp_path: Path, stdout: str, *, jar_name: str = "freerouting-2.2.4.jar",
         ses_vias: int = 0, via_costs=None, timeout: int = 300) -> dict:
    java = tmp_path / "java.exe"
    java.touch()
    jar = tmp_path / jar_name
    jar.touch()
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(PCB)", encoding="utf-8")
    ses = tmp_path / "board.ses"
    ses.write_text("(session\n" + "  (via \"V\" 0 0)\n" * ses_vias + ")\n", encoding="utf-8")

    proc = MagicMock()
    proc.communicate.return_value = (stdout.encode(), b"")

    with patch("kicad_mcp.tools.routing.find_java", return_value=java), \
         patch("kicad_mcp.tools.routing.find_freerouting_jar", return_value=jar), \
         patch("kicad_mcp.utils.platform_helper.detect_java_major_version", return_value=25), \
         patch("kicad_mcp.tools.routing.subprocess.Popen", return_value=proc) as popen:
        out = _impl_run_freerouter(
            dsn_path=str(dsn), output=str(ses), max_passes=3,
            freerouting_jar="", java_path="",
            config=_cfg(timeout), change_log=ChangeLog(tmp_path / "c.json"),
            via_costs=via_costs,
        )
    result = json.loads(out)
    result["_popen"] = popen
    result["_proc"] = proc
    return result


def test_incomplete_not_success(tmp_path: Path):
    r = _run(tmp_path, _V2_STDOUT_7_UNROUTED)
    assert r["status"] == "incomplete"
    assert r["unrouted"] == 7  # the FINAL (last) count, not pass #1's 29


def test_complete_is_success_zero_unrouted(tmp_path: Path):
    r = _run(tmp_path, _V2_STDOUT_COMPLETE)
    assert r["status"] == "success"
    assert r["unrouted"] == 0


def test_reports_version_passes_elapsed(tmp_path: Path):
    r = _run(tmp_path, _V2_STDOUT_COMPLETE)
    assert r["detected_version"] == "2.2.4"
    assert r["max_passes"] == 3
    assert "elapsed_s" in r


def test_vias_parsed_from_ses(tmp_path: Path):
    r = _run(tmp_path, _V2_STDOUT_COMPLETE, ses_vias=5)
    assert r["vias"] == 5


def test_v1_no_count_is_unverified_not_false_success(tmp_path: Path):
    r = _run(tmp_path, _V1_STDOUT, jar_name="freerouting-1.9.0.jar")
    assert r["status"] == "success"
    assert r["unrouted"] is None
    assert r["completeness"] == "unverified"
    assert r["detected_version"] == "1.9.0"


def test_via_costs_flag_injected_only_for_v2(tmp_path: Path):
    r2 = _run(tmp_path, _V2_STDOUT_COMPLETE, via_costs=200)
    cmd2 = r2["_popen"].call_args.args[0]
    assert "--router.scoring.via_costs=200" in cmd2
    assert r2["via_costs"] == 200

    r1 = _run(tmp_path, _V1_STDOUT, jar_name="freerouting-1.9.0.jar", via_costs=200)
    cmd1 = r1["_popen"].call_args.args[0]
    # Match the actual flag, not a bare "via_costs" substring — the pytest
    # tmp_path dir is named after this test, so path args contain "via_costs".
    assert not any(a.startswith("--router.scoring.via_costs") for a in cmd1)
