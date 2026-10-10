"""TraceMaker Default Routing Stage 1 (unit 3) — autoroute router selection.

The ``router`` parameter ("auto" | "tracemaker" | "freerouting") selects the
routing engine: "auto" prefers a detected TraceMaker runtime and falls back
to FreeRouting — including mid-run on any TraceMaker failure (run error or
timeout, missing/invalid output, extraction failure, import exception), with
a ``router_fallback`` step recorded; explicit values force the engine (forced
TraceMaker fails with a structured error instead of falling back).

Unit boundaries (units 1 and 2 are stubs in this worktree) are mocked at the
modules routing.py imports from: ``kicad_mcp.utils.tracemaker_runtime``
(detect_tracemaker / run_tracemaker) and ``kicad_mcp.tools.routing_import``
(extract_new_routes / import_routed_tracks_with_fallback). The FreeRouting
internals are mocked exactly like tests/test_autoroute_robustness.py, whose
harness this file reuses.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kicad_mcp.tools import routing
from kicad_mcp.utils.tracemaker_runtime import TracemakerRuntime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BOARD_NO_ROUTES = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
      )
    )
""")

BOARD_WITH_ZONE = BOARD_NO_ROUTES.replace(
    "  (footprint",
    "  (zone (net 0) (net_name \"GND\") (layer \"B.Cu\") (uuid \"z1\")\n"
    "    (name \"gnd_pour\")\n"
    "    (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40)))\n"
    "  )\n"
    "  (footprint",
)

VALID_SES = textwrap.dedent("""\
    (session test
      (base_design test)
      (routes
        (network_out
          (net "Net-(C1-Pad1)"
            (wire (path F.Cu 2500 10 10 20 20))
          )
        )
      )
    )
""")

PRO_TEXT = '{"board": {"design_settings": {"rules": {"min_clearance": 0.2}}}}'

RUNTIME = TracemakerRuntime(
    mode="wsl",
    distro="tracemaker-build",
    binary="/usr/local/bin/tracemaker",
    version="0.9.1",
)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_NO_ROUTES, encoding="utf-8")
    return f


@pytest.fixture
def board_with_pro(board: Path) -> Path:
    board.with_suffix(".kicad_pro").write_text(PRO_TEXT, encoding="utf-8")
    return board


@pytest.fixture
def zoned_board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_WITH_ZONE, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Harness — same shape as test_autoroute_robustness.py, extended with the
# TraceMaker seams (units 1/2 mocked at their source modules).
# ---------------------------------------------------------------------------

def _autoroute(
    board: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    detect: TracemakerRuntime | None = None,
    detect_raises: BaseException | None = None,
    tm_run_result: dict[str, Any] | None = None,
    tm_write_output: bool = True,
    tm_run_raises: BaseException | None = None,
    tm_extract_raises: BaseException | None = None,
    tm_import_raises: BaseException | None = None,
    tm_import_result: dict[str, Any] | None = None,  # exercised: tracks_added test
    tm_calls: dict[str, Any] | None = None,
    freerouting_single_result: dict[str, Any] | None = None,
    backend: MagicMock | None = None,
    **tool_kwargs: Any,
) -> dict[str, Any]:
    """Drive the registered autoroute tool with scripted engine internals.

    detect/detect_raises: what detect_tracemaker returns/raises.
    tm_*: scripted behavior of run_tracemaker / extract_new_routes /
    import_routed_tracks_with_fallback. tm_calls (when given) records what
    each seam saw. The FreeRouting leg is a single (non-ladder) scripted run
    that writes VALID_SES and imports 10 tracks.
    """
    import fastmcp
    from kicad_mcp.utils.change_log import ChangeLog

    calls = tm_calls if tm_calls is not None else {}

    if backend is None:
        backend = MagicMock()
        backend.get_board_modify_ops.return_value.clear_routes.return_value = {
            "status": "success", "tracks_removed": 0, "vias_removed": 0,
            "backup_path": str(board) + ".bak",
        }
    backend.get_drc_ops.return_value.run_drc.return_value = {
        "passed": True, "error_count": 0, "warning_count": 0,
    }

    # Gates green (validation-cache mechanism bypassed, as in the robustness
    # harness) + clean step scripted.
    monkeypatch.setattr("kicad_mcp.utils.gates.check_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        "kicad_mcp.utils.validation_cache.get_validation",
        lambda *a, **k: {"checked": 0},
    )
    monkeypatch.setattr(
        routing, "_resolve_routing_runtime", lambda *a, **k: {"resolved": "test"}
    )
    monkeypatch.setattr(
        routing, "_impl_clean_board_for_routing",
        lambda *a, **k: json.dumps(
            {"status": "success", "keepouts_removed": 0, "tracks_removed": 0}
        ),
    )

    # --- Unit 1 seam: kicad_mcp.utils.tracemaker_runtime -------------------
    def fake_detect(force: bool = False):
        calls["detect_called"] = calls.get("detect_called", 0) + 1
        if detect_raises is not None:
            raise detect_raises
        return detect

    monkeypatch.setattr(
        "kicad_mcp.utils.tracemaker_runtime.detect_tracemaker", fake_detect
    )

    def fake_run_tracemaker(rt, pcb_in: Path, pcb_out: Path, **kwargs):
        calls["run_rt"] = rt
        calls["run_pcb_in"] = Path(pcb_in)
        calls["run_pcb_out"] = Path(pcb_out)
        calls["run_in_exists"] = Path(pcb_in).exists()
        calls["run_out_preexists"] = Path(pcb_out).exists()
        pro_copy = Path(pcb_in).with_suffix(".kicad_pro")
        calls["run_pro_exists"] = pro_copy.exists()
        calls["run_pro_text"] = (
            pro_copy.read_text(encoding="utf-8") if pro_copy.exists() else None
        )
        if tm_run_raises is not None:
            raise tm_run_raises
        if tm_write_output:
            Path(pcb_out).write_text("(kicad_pcb routed)", encoding="utf-8")
        return {
            "status": "success", "returncode": 0, "wall_time_s": 1.4,
            "stdout_tail": "done", "stderr_tail": "", "unrouted": 0,
            **(tm_run_result or {}),
        }

    monkeypatch.setattr(
        "kicad_mcp.utils.tracemaker_runtime.run_tracemaker", fake_run_tracemaker
    )

    # --- Unit 2 seam: kicad_mcp.tools.routing_import ------------------------
    def fake_extract(input_pcb: Path, routed_pcb: Path):
        calls["extract_args"] = (Path(input_pcb), Path(routed_pcb))
        if tm_extract_raises is not None:
            raise tm_extract_raises
        # Real extract_new_routes contract: the route LISTS, not counts
        # (routing.py derives the counts via len() — the 2026-10-08 live run
        # exposed a key-name mismatch here when both sides were faked).
        return {
            "segments": [{"start_x": 0.0}] * 11,
            "vias": [{"x": 0.0}] * 2,
        }

    monkeypatch.setattr(
        "kicad_mcp.tools.routing_import.extract_new_routes", fake_extract
    )

    def fake_tm_import(backend_, board_: Path, input_copy: Path, routed_pcb: Path):
        calls["import_args"] = (Path(board_), Path(input_copy), Path(routed_pcb))
        if tm_import_raises is not None:
            raise tm_import_raises
        return dict(tm_import_result or {"status": "success", "new_tracks": 12})

    monkeypatch.setattr(
        "kicad_mcp.tools.routing_import.import_routed_tracks_with_fallback",
        fake_tm_import,
    )

    # --- FreeRouting leg (single run, as in the robustness harness) ---------
    def fake_export(backend_, p, dsn_path):
        dsn_path.write_text("(pcb fake)", encoding="utf-8")
        return {"dsn_path": str(dsn_path)}

    monkeypatch.setattr(routing, "_export_dsn_with_fallback", fake_export)
    monkeypatch.setattr(routing, "_detect_freerouting_is_v2", lambda *a, **k: False)

    def fake_run(dsn_path, output, max_passes, jar, java, config, change_log,
                 via_costs=None):
        Path(output).write_text(VALID_SES, encoding="utf-8")
        result = {"status": "success", "unrouted": 0, "vias": 1,
                  **(freerouting_single_result or {})}
        return json.dumps(result)

    monkeypatch.setattr(routing, "_impl_run_freerouter", fake_run)

    def fake_import(backend_, p, ses_path):
        return {"success": True, "tracks_before": 0, "tracks_after": 10,
                "new_tracks": 10, "via": "subprocess"}

    monkeypatch.setattr(routing, "_import_ses_with_fallback", fake_import)

    mcp = fastmcp.FastMCP("test")
    change_log = ChangeLog(board.parent / "changes.json")
    routing.register_tools(mcp, backend, change_log, config={})
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "autoroute"
    )
    return json.loads(tool_fn(str(board), **tool_kwargs))


def _step(report: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((s for s in report["steps"] if s["step"] == name), None)


def _step_names(report: dict[str, Any]) -> list[str]:
    return [s["step"] for s in report["steps"]]


def _tm_dir(board: Path) -> Path:
    return board.parent / ".tracemaker"


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

def test_unknown_router_structured_error_nothing_touched(board, monkeypatch):
    calls: dict[str, Any] = {}
    before = board.read_text(encoding="utf-8")
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls,
                        router="topological")
    assert report["status"] == "error"
    assert report["error_code"] == "unknown_router"
    assert "topological" in report["message"]
    assert "freerouting" in report["message"]
    assert report["steps"] == []  # refused before gates — nothing ran
    assert "detect_called" not in calls
    assert board.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_auto_with_detected_runtime_routes_via_tracemaker(board, monkeypatch):
    calls: dict[str, Any] = {}
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls)
    assert report["status"] == "success"
    pre = _step(report, "routing_preflight")
    assert pre["router"] == "tracemaker"
    assert pre["router_resolution"] == {
        "mode": "wsl", "distro": "tracemaker-build",
        "binary": "/usr/local/bin/tracemaker", "version": "0.9.1",
    }
    assert calls["run_rt"] is RUNTIME
    assert _step(report, "run_tracemaker") is not None
    assert _step(report, "import_routed_tracks")["status"] == "success"
    # Selection after the gates, before any routing work.
    names = _step_names(report)
    assert names.index("placement_quality_gate") < names.index("routing_preflight")
    assert "run_freerouter" not in names and "export_dsn" not in names


def test_auto_without_runtime_routes_via_freerouting(board, monkeypatch):
    calls: dict[str, Any] = {}
    report = _autoroute(board, monkeypatch, detect=None, tm_calls=calls)
    assert report["status"] == "success"
    assert calls["detect_called"] == 1
    pre = _step(report, "routing_preflight")
    assert pre["router"] == "freerouting"
    assert pre["resolved"] == "test"  # existing FreeRouting resolution fields
    assert "router_resolution" not in pre
    names = _step_names(report)
    assert "run_freerouter" in names and "import_ses" in names
    assert "run_tracemaker" not in names


def test_auto_detection_exception_falls_back_to_freerouting(board, monkeypatch):
    # A broken detector must never break routing (also keeps the unit-1 stub,
    # whose detect raises NotImplementedError, harmless pre-integration).
    report = _autoroute(board, monkeypatch,
                        detect_raises=NotImplementedError("stub"))
    assert report["status"] == "success"
    assert _step(report, "routing_preflight")["router"] == "freerouting"


def test_explicit_tracemaker_unavailable_structured_error(board, monkeypatch):
    before = board.read_text(encoding="utf-8")
    report = _autoroute(board, monkeypatch, detect=None, router="tracemaker")
    assert report["status"] == "error"
    assert report["error_code"] == "tracemaker_unavailable"
    assert "no TraceMaker runtime was detected" in report["message"]
    assert "detection failed" not in report["message"]
    for env in ("KICAD_MCP_TRACEMAKER_DISTRO", "KICAD_MCP_TRACEMAKER_BIN",
                "KICAD_MCP_NO_TRACEMAKER"):
        assert env in report["message"]
    # Gates ran; no routing work (selection errors before clear/clean).
    assert _step_names(report) == [
        "connector_orientation_gate", "placement_quality_gate",
    ]
    assert board.read_text(encoding="utf-8") == before


def test_explicit_freerouting_ignores_detection(board, monkeypatch):
    calls: dict[str, Any] = {}
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls,
                        router="freerouting")
    assert report["status"] == "success"
    assert "detect_called" not in calls  # detection skipped entirely
    assert _step(report, "routing_preflight")["router"] == "freerouting"
    assert "run_tracemaker" not in _step_names(report)


# ---------------------------------------------------------------------------
# TraceMaker success path
# ---------------------------------------------------------------------------

def test_tracemaker_success_report_and_cleanup(board_with_pro, monkeypatch):
    calls: dict[str, Any] = {}
    report = _autoroute(board_with_pro, monkeypatch, detect=RUNTIME,
                        tm_calls=calls)
    assert report["status"] == "success"
    assert report["message"] == "Auto-routing complete: 12 tracks routed"
    run = _step(report, "run_tracemaker")
    assert run["status"] == "success" and run["wall_time_s"] == 1.4
    imp = _step(report, "import_routed_tracks")
    assert imp["status"] == "success" and imp["new_tracks"] == 12
    # Tail steps run for the TraceMaker engine too.
    names = _step_names(report)
    assert "post_route_drc" in names
    assert names.index("import_routed_tracks") < names.index("post_route_drc")
    # Run-dir copies cleaned up on success — and the empty run dir removed.
    d = _tm_dir(board_with_pro)
    assert not d.exists()
    # Import worked on the real board with the run-dir copies.
    assert calls["import_args"] == (
        board_with_pro, d / "tracemaker_in.kicad_pcb",
        d / "tracemaker_out.kicad_pcb",
    )
    # Unverified-count flag absent when TraceMaker reported unrouted == 0.
    assert "unrouted_unverified" not in report
    assert "unverified" not in report["message"]


def test_tracemaker_run_dir_copies_include_project_file(board_with_pro,
                                                        monkeypatch):
    calls: dict[str, Any] = {}
    _autoroute(board_with_pro, monkeypatch, detect=RUNTIME, tm_calls=calls)
    d = _tm_dir(board_with_pro)
    assert calls["run_pcb_in"] == d / "tracemaker_in.kicad_pcb"
    assert calls["run_pcb_out"] == d / "tracemaker_out.kicad_pcb"
    assert calls["run_in_exists"] is True
    # Rules travel with the board: the .kicad_pro copy sits beside the input.
    assert calls["run_pro_exists"] is True
    assert calls["run_pro_text"] == PRO_TEXT


def test_tracemaker_no_project_file_runs_bare(board, monkeypatch):
    calls: dict[str, Any] = {}
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls)
    assert report["status"] == "success"
    assert calls["run_pro_exists"] is False


def test_tracemaker_stale_output_removed_before_run(board, monkeypatch):
    d = _tm_dir(board)
    d.mkdir()
    stale = d / "tracemaker_out.kicad_pcb"
    stale.write_text("(kicad_pcb stale)", encoding="utf-8")
    calls: dict[str, Any] = {}
    # This run's TraceMaker writes NOTHING — without the stale guard the old
    # file would be taken for fresh output instead of failing over.
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls,
                        tm_write_output=False)
    assert calls["run_out_preexists"] is False  # guard deleted it pre-launch
    fb = _step(report, "router_fallback")
    assert fb is not None and "no output board" in fb["reason"]
    assert report["status"] == "success"  # FreeRouting fallback completed


def test_tracemaker_partial_when_unrouted_remains(board, monkeypatch):
    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_run_result={"unrouted": 3})
    assert report["status"] == "partial"
    assert report["message"] == (
        "Auto-routing incomplete: 12 tracks routed, 3 connection(s) still "
        "unrouted — raise max_passes or review placement."
    )
    assert _step(report, "router_fallback") is None


def test_tracemaker_tail_refills_zones_before_drc(zoned_board, monkeypatch):
    refills: list[Path] = []

    def fake_refill(p, backend):
        refills.append(p)
        return {"status": "success", "via": "file", "zones_filled": 1}

    monkeypatch.setattr("kicad_mcp.tools.board._impl_refill_zones", fake_refill)
    report = _autoroute(zoned_board, monkeypatch, detect=RUNTIME)
    assert report["status"] == "success"
    assert refills == [zoned_board]
    names = _step_names(report)
    assert names.index("import_routed_tracks") < names.index("zone_refill")
    assert names.index("zone_refill") < names.index("post_route_drc")


# ---------------------------------------------------------------------------
# Failure modes — auto falls back, explicit tracemaker refuses
# ---------------------------------------------------------------------------

FAILURE_MODES: list[tuple[str, dict[str, Any], str]] = [
    ("run_error", {"tm_run_result": {"status": "error", "returncode": 1}},
     "status 'error'"),
    ("run_timeout", {"tm_run_result": {"status": "timeout", "returncode": -9}},
     "status 'timeout'"),
    ("run_raises", {"tm_run_raises": OSError("wsl.exe not found")},
     "run_tracemaker raised"),
    ("no_output", {"tm_write_output": False}, "no output board"),
    ("extract_fails", {"tm_extract_raises": ValueError("no new routes")},
     "routed-output validation failed"),
    ("import_fails",
     {"tm_import_raises": RuntimeError("bridge import exploded")},
     "routed-track import failed"),
]


@pytest.mark.parametrize(
    "mode,kwargs,reason_frag", FAILURE_MODES, ids=[m[0] for m in FAILURE_MODES]
)
def test_auto_tracemaker_failure_falls_back_to_freerouting(
    board_with_pro, monkeypatch, mode, kwargs, reason_frag,
):
    report = _autoroute(board_with_pro, monkeypatch, detect=RUNTIME, **kwargs)
    fb = _step(report, "router_fallback")
    assert fb is not None
    assert fb["from"] == "tracemaker" and fb["to"] == "freerouting"
    assert reason_frag in fb["reason"]
    assert fb["resolved"] == "test"  # FreeRouting resolution fields on fallback
    names = _step_names(report)
    # The failing step precedes the fallback; the FreeRouting pipeline follows.
    assert names.index("router_fallback") < names.index("run_freerouter")
    assert "import_ses" in names
    assert report["status"] == "success"
    assert report["message"] == "Auto-routing complete: 10 tracks routed"
    # Run dir + copies kept for diagnosis and named in the report.
    assert _tm_dir(board_with_pro).exists()
    preserved = report["tracemaker_preserved"]
    assert str(_tm_dir(board_with_pro) / "tracemaker_in.kicad_pcb") in preserved
    for f in preserved:
        assert Path(f).exists()


@pytest.mark.parametrize(
    "mode,kwargs,reason_frag", FAILURE_MODES, ids=[m[0] for m in FAILURE_MODES]
)
def test_explicit_tracemaker_failure_is_structured_error(
    board_with_pro, monkeypatch, mode, kwargs, reason_frag,
):
    report = _autoroute(board_with_pro, monkeypatch, detect=RUNTIME,
                        router="tracemaker", **kwargs)
    assert report["status"] == "error"
    assert report["error_code"] == "tracemaker_failed"
    assert reason_frag in report["message"]
    assert "preserved" in report["message"]
    names = _step_names(report)
    assert "router_fallback" not in names
    assert "run_freerouter" not in names and "export_dsn" not in names
    for f in report["tracemaker_preserved"]:
        assert Path(f).exists()


def test_failure_steps_recorded_with_errors(board, monkeypatch):
    # Each failure mode records its failing step with the error text.
    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_extract_raises=ValueError("zero routes"))
    ext = _step(report, "validate_output")
    assert ext["status"] == "error" and "zero routes" in ext["error"]

    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_import_raises=RuntimeError("boom"))
    imp = _step(report, "import_routed_tracks")
    assert imp["status"] == "error" and "boom" in imp["error"]

    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_run_raises=OSError("no wsl"))
    run = _step(report, "run_tracemaker")
    assert run["status"] == "error" and "no wsl" in run["error"]


# ---------------------------------------------------------------------------
# Review fixes (APPROVE-WITH-NITS round)
# ---------------------------------------------------------------------------

def test_tracemaker_unverified_unrouted_flagged(board, monkeypatch):
    # Fix 1: unrouted=None keeps the success mirror rule but is never a
    # silently unverified success — flag + message suffix, DRC stays the oracle.
    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_run_result={"unrouted": None})
    assert report["status"] == "success"
    assert report["unrouted_unverified"] is True
    assert report["message"] == (
        "Auto-routing complete: 12 tracks routed "
        "(unrouted count unverified — confirm with post-route DRC)"
    )
    assert _step(report, "post_route_drc") is not None


def test_stale_pro_copy_removed_when_project_has_none(board, monkeypatch):
    # Fix 2: a leftover tracemaker_in.kicad_pro from an earlier run (project
    # since lost its .kicad_pro) must never reach this run's TraceMaker.
    d = _tm_dir(board)
    d.mkdir()
    (d / "tracemaker_in.kicad_pro").write_text('{"stale": true}',
                                               encoding="utf-8")
    calls: dict[str, Any] = {}
    report = _autoroute(board, monkeypatch, detect=RUNTIME, tm_calls=calls)
    assert report["status"] == "success"
    assert calls["run_pro_exists"] is False  # stale copy gone at run time
    assert calls["run_pro_text"] is None


def test_explicit_tracemaker_detection_crash_reports_true_cause(board,
                                                                monkeypatch):
    # Fix 3: a crashed detector must not masquerade as "no runtime detected".
    report = _autoroute(board, monkeypatch,
                        detect_raises=RuntimeError("wsl.exe exploded"),
                        router="tracemaker")
    assert report["status"] == "error"
    assert report["error_code"] == "tracemaker_unavailable"
    assert "TraceMaker detection failed: wsl.exe exploded" in report["message"]
    assert "no TraceMaker runtime was detected" not in report["message"]
    assert "KICAD_MCP_TRACEMAKER_DISTRO" in report["message"]


def test_validate_output_success_step_between_run_and_import(board,
                                                             monkeypatch):
    # Fix 4: the spec's run → validation → import sequence is visible in the
    # report on success too, with the extraction counts.
    report = _autoroute(board, monkeypatch, detect=RUNTIME)
    val = _step(report, "validate_output")
    assert val == {
        "step": "validate_output", "status": "success",
        "new_segments": 11, "new_vias": 2,
    }
    names = _step_names(report)
    assert (names.index("run_tracemaker") < names.index("validate_output")
            < names.index("import_routed_tracks"))


def test_tracemaker_run_dir_removed_on_success_kept_on_failure(board,
                                                               monkeypatch):
    # Fix 5: empty .tracemaker/ is tidied away on success...
    report = _autoroute(board, monkeypatch, detect=RUNTIME)
    assert report["status"] == "success"
    assert not _tm_dir(board).exists()
    # ...and kept (with the copies) on a forced-TraceMaker failure.
    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        router="tracemaker", tm_write_output=False)
    assert report["status"] == "error"
    assert _tm_dir(board).exists()


def test_import_result_tracks_added_key_used(board, monkeypatch):
    # Fix 6: an import result carrying only "tracks_added" (no "new_tracks")
    # still feeds the success message.
    report = _autoroute(board, monkeypatch, detect=RUNTIME,
                        tm_import_result={"status": "success",
                                          "tracks_added": 7})
    assert report["status"] == "success"
    assert report["message"] == "Auto-routing complete: 7 tracks routed"
    assert _step(report, "import_routed_tracks")["tracks_added"] == 7
