"""REQ-FF-1..5 (KIR3 S6, F16/F17) — autoroute routing robustness.

F16: re-running autoroute must not stack tracks — existing routes are cleared
first (with backup + report), with the pipeline-wide fallback set (a backend
with no live board ops must not break the pipeline). F17: a truncated SES
(timeout kill mid-write) never reaches import; import failures are structured,
preserve the DSN/SES artifacts, and retry the next-best valid rung.

The pipeline internals (clean/export/FreeRouting/import) are monkeypatched at
the module boundary; the clear step exercises the REAL FileBoardOps fallback
on real board text. LC-1 ground truth (verified pcbnew 9.0.7 + 10.0.4):
ImportSpecctraSES returns false on a truncated SES without mutating the board,
and imports a well-formed partial session fine.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kicad_mcp.tools import routing
from kicad_mcp.tools.routing import _rank_via_ladder, _ses_well_formed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BOARD_WITH_ROUTES = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "GND")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
      )
      (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1))
      (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 1))
      (via (at 15 15) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
    )
""")

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

# Quoted net names carry parens (LC-0: 66 such strings in a real SES) — the
# well-formedness scan must be quote-aware.
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

# A net name with an UNBALANCED paren inside quotes — legal SES content that a
# naive (non-quote-aware) paren counter would reject.
VALID_SES_UNBALANCED_IN_QUOTES = VALID_SES.replace('"Net-(C1-Pad1)"', '"Net-(C1"')

TRUNCATED_SES = VALID_SES[: len(VALID_SES) // 2]


@pytest.fixture
def routed_board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_WITH_ROUTES, encoding="utf-8")
    return f


@pytest.fixture
def unrouted_board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_NO_ROUTES, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# _ses_well_formed (REQ-FF-3)
# ---------------------------------------------------------------------------

def test_ses_well_formed_valid(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text(VALID_SES, encoding="utf-8")
    assert _ses_well_formed(f) is True


def test_ses_well_formed_quote_aware(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text(VALID_SES_UNBALANCED_IN_QUOTES, encoding="utf-8")
    assert _ses_well_formed(f) is True


def test_ses_well_formed_truncated(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text(TRUNCATED_SES, encoding="utf-8")
    assert _ses_well_formed(f) is False


def test_ses_well_formed_empty(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text("", encoding="utf-8")
    assert _ses_well_formed(f) is False


def test_ses_well_formed_missing(tmp_path: Path):
    assert _ses_well_formed(tmp_path / "nope.ses") is False


def test_ses_well_formed_wrong_header(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text("(kicad_pcb (version 1))", encoding="utf-8")
    assert _ses_well_formed(f) is False


def test_ses_well_formed_extra_close(tmp_path: Path):
    f = tmp_path / "a.ses"
    f.write_text("(session x))", encoding="utf-8")
    assert _ses_well_formed(f) is False


# ---------------------------------------------------------------------------
# _rank_via_ladder ordering (REQ-FF-5 retry order; winner semantics pinned by
# test_via_ladder.py)
# ---------------------------------------------------------------------------

def test_rank_complete_before_incomplete():
    rungs = [
        {"via_costs": 50, "unrouted": 2, "vias": 1, "track_length": 1.0, "ses_path": "a"},
        {"via_costs": 100, "unrouted": 0, "vias": 9, "track_length": 9.0, "ses_path": "b"},
        {"via_costs": 150, "unrouted": 0, "vias": 3, "track_length": 3.0, "ses_path": "c"},
    ]
    ranked = _rank_via_ladder(rungs)
    assert [r["via_costs"] for r in ranked] == [150, 100, 50]


def test_rank_incomplete_by_fewest_unrouted():
    rungs = [
        {"via_costs": 50, "unrouted": 5, "vias": 1, "track_length": 1.0, "ses_path": "a"},
        {"via_costs": 100, "unrouted": 2, "vias": 9, "track_length": 9.0, "ses_path": "b"},
        {"via_costs": 150, "unrouted": None, "vias": 2, "track_length": 2.0, "ses_path": "c"},
    ]
    ranked = _rank_via_ladder(rungs)
    assert [r["via_costs"] for r in ranked] == [100, 50, 150]


# ---------------------------------------------------------------------------
# Autoroute pipeline harness
# ---------------------------------------------------------------------------

def _autoroute(
    board: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_v2: bool = True,
    ses_by_via_cost: dict[int, str | None] | None = None,
    single_ses: str | None = None,
    run_results: dict[int, dict[str, Any]] | None = None,
    single_result: dict[str, Any] | None = None,
    import_fail_paths: set[str] | None = None,
    import_fail_all: bool = False,
    backend: MagicMock | None = None,
    **tool_kwargs: Any,
) -> dict[str, Any]:
    """Drive the registered autoroute tool with scripted pipeline internals.

    ses_by_via_cost: via_cost -> SES text to write (None = write nothing).
    run_results: via_cost -> extra keys for the run_freerouter JSON.
    import_fail_paths: SES path substrings whose import raises.
    """
    import fastmcp
    from kicad_mcp.utils.change_log import ChangeLog

    if backend is None:
        backend = MagicMock()
        backend.get_board_modify_ops.return_value.clear_routes.return_value = {
            "status": "success", "tracks_removed": 0, "vias_removed": 0,
            "backup_path": str(board) + ".bak",
        }
    backend.get_drc_ops.return_value.run_drc.return_value = {
        "passed": True, "error_count": 0, "warning_count": 0,
    }

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

    def fake_export(backend_, p, dsn_path):
        dsn_path.write_text("(pcb fake)", encoding="utf-8")
        return {"dsn_path": str(dsn_path)}

    monkeypatch.setattr(routing, "_export_dsn_with_fallback", fake_export)
    monkeypatch.setattr(
        routing, "_detect_freerouting_is_v2", lambda *a, **k: is_v2
    )

    def fake_run(dsn_path, output, max_passes, jar, java, config, change_log,
                 via_costs=None):
        if via_costs is not None:
            content = (ses_by_via_cost or {}).get(via_costs)
            extra = (run_results or {}).get(via_costs, {})
        else:
            content = single_ses
            extra = single_result or {}
        if content is not None:
            Path(output).write_text(content, encoding="utf-8")
        result = {"status": "success", "unrouted": 0, "vias": 1, **extra}
        return json.dumps(result)

    monkeypatch.setattr(routing, "_impl_run_freerouter", fake_run)

    def fake_import(backend_, p, ses_path):
        if import_fail_all or any(
            frag in str(ses_path) for frag in (import_fail_paths or set())
        ):
            raise RuntimeError(
                f"Plugin bridge error: ImportSpecctraSES failed for {ses_path}"
            )
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


# ---------------------------------------------------------------------------
# F16 — clear-before-reroute (REQ-FF-1/2)
# ---------------------------------------------------------------------------

def test_clear_step_runs_before_clean_and_reports(routed_board, monkeypatch):
    backend = MagicMock()
    backend.get_board_modify_ops.return_value.clear_routes.return_value = {
        "status": "success", "tracks_removed": 7, "vias_removed": 2,
        "backup_path": "b.kicad_pcb",
    }
    report = _autoroute(routed_board, monkeypatch, backend=backend,
                        ses_by_via_cost={50: VALID_SES})
    steps = [s["step"] for s in report["steps"]]
    assert "clear_routes" in steps and "clean_board" in steps
    assert steps.index("clear_routes") < steps.index("clean_board")
    clear = _step(report, "clear_routes")
    assert clear["status"] == "success"
    assert clear["tracks_removed"] == 7 and clear["vias_removed"] == 2
    assert clear["backup_path"] == "b.kicad_pcb"
    backend.get_board_modify_ops.return_value.clear_routes.assert_called_once()


def test_clear_step_file_fallback_on_notimplemented(routed_board, monkeypatch):
    # The gate-test harness shape: no live board ops at all. The clear must
    # degrade to the REAL file backend, not break the pipeline (review
    # finding 1).
    backend = MagicMock()
    backend.get_board_modify_ops.side_effect = NotImplementedError
    report = _autoroute(routed_board, monkeypatch, backend=backend,
                        ses_by_via_cost={50: VALID_SES})
    clear = _step(report, "clear_routes")
    assert clear is not None and clear["status"] == "success"
    assert clear["tracks_removed"] == 2 and clear["vias_removed"] == 1
    content = routed_board.read_text(encoding="utf-8")
    assert "(segment" not in content and "(via (at" not in content


def test_clear_step_file_fallback_on_boardless_bridge(routed_board, monkeypatch):
    backend = MagicMock()
    backend.get_board_modify_ops.return_value.clear_routes.side_effect = (
        RuntimeError("Plugin bridge error: Requested board 'x' does not match "
                     "open board ''")
    )
    report = _autoroute(routed_board, monkeypatch, backend=backend,
                        ses_by_via_cost={50: VALID_SES})
    clear = _step(report, "clear_routes")
    assert clear is not None and clear["status"] == "success"
    assert clear["tracks_removed"] == 2


def test_optout_warns_on_routed_board(routed_board, monkeypatch):
    before = routed_board.read_text(encoding="utf-8")
    report = _autoroute(routed_board, monkeypatch,
                        ses_by_via_cost={50: VALID_SES},
                        clear_routed_tracks=False)
    clear = _step(report, "clear_routes")
    assert clear is not None and clear["status"] == "skipped"
    assert clear["existing_tracks"] == 3  # 2 segments + 1 via
    assert "stack" in clear["warning"]
    assert routed_board.read_text(encoding="utf-8") == before


def test_optout_silent_on_unrouted_board(unrouted_board, monkeypatch):
    report = _autoroute(unrouted_board, monkeypatch,
                        ses_by_via_cost={50: VALID_SES},
                        clear_routed_tracks=False)
    assert _step(report, "clear_routes") is None


# ---------------------------------------------------------------------------
# F17 — ladder admission, stale guard, retry, structured failure (REQ-FF-3/4/5)
# ---------------------------------------------------------------------------

def test_ladder_excludes_truncated_ses(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: TRUNCATED_SES, 100: VALID_SES},
        via_cost_ladder=[50, 100],
    )
    assert report["status"] == "success"
    assert report["via_ladder"]["tried"] == [100]
    assert report["via_ladder"]["selected_via_cost"] == 100
    fr_steps = [s for s in report["steps"] if s["step"] == "run_freerouter"]
    assert fr_steps[0].get("invalid_ses") is True
    assert "invalid_ses" not in fr_steps[1]


def test_stale_ses_from_previous_run_never_admitted(unrouted_board, monkeypatch):
    # A preserved SES from a previous failed run sits at the deterministic rung
    # path; this run's FreeRouting writes nothing for that rung. Without the
    # pre-launch unlink the stale file would be admitted as fresh (review
    # finding 2).
    stale = unrouted_board.parent / "freerouting_vc50.ses"
    stale.write_text(VALID_SES, encoding="utf-8")
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: None, 100: VALID_SES},  # vc50 writes NOTHING
        via_cost_ladder=[50, 100],
    )
    assert report["status"] == "success"
    assert report["via_ladder"]["tried"] == [100]


def test_winner_import_failure_falls_to_next_rung(unrouted_board, monkeypatch):
    # vc50 wins (fewer vias); its import fails; vc100 must be imported instead
    # and the routed/unrouted truth must come from vc100 (REQ-FF-5).
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: VALID_SES, 100: VALID_SES},
        run_results={50: {"unrouted": 0, "vias": 1},
                     100: {"unrouted": 0, "vias": 5}},
        import_fail_paths={"vc50"},
        via_cost_ladder=[50, 100],
    )
    assert report["status"] == "success"
    assert report["via_ladder"]["selected_via_cost"] == 50
    assert report["via_ladder"]["imported_via_cost"] == 100
    imp = _step(report, "import_ses")
    assert imp["status"] == "success"
    assert len(imp["failed_attempts"]) == 1
    assert imp["failed_attempts"][0]["via_costs"] == 50


def test_all_imports_fail_structured_error_preserves_artifacts(
    unrouted_board, monkeypatch,
):
    before = unrouted_board.read_text(encoding="utf-8")
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: VALID_SES, 100: VALID_SES},
        import_fail_all=True,
        via_cost_ladder=[50, 100],
    )
    assert report["status"] == "error"
    assert report["error_code"] == "ses_import_failed"
    assert report["board_modified"] is False
    assert len(report["import_attempts"]) == 2
    assert all("ImportSpecctraSES failed" in a["error"]
               for a in report["import_attempts"])
    # Artifacts preserved on disk, board byte-identical.
    assert report["ses_preserved"]
    for s in report["ses_preserved"]:
        assert Path(s).exists()
    assert report["dsn_preserved"] and Path(report["dsn_preserved"]).exists()
    assert unrouted_board.read_text(encoding="utf-8") == before


def test_no_valid_session_error_preserves_artifacts(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: TRUNCATED_SES, 100: TRUNCATED_SES},
        via_cost_ladder=[50, 100],
    )
    assert report["status"] == "error"
    assert report["error_code"] == "no_valid_session"
    assert len(report["ses_preserved"]) == 2
    for s in report["ses_preserved"]:
        assert Path(s).exists()
    assert report["dsn_preserved"] and Path(report["dsn_preserved"]).exists()


def test_single_run_invalid_ses_structured_error(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        is_v2=False,
        single_ses=TRUNCATED_SES,
    )
    assert report["status"] == "error"
    assert report["error_code"] == "no_valid_session"
    fr = _step(report, "run_freerouter")
    assert fr.get("invalid_ses") is True
    assert report["ses_preserved"]


def test_single_run_valid_partial_still_partial(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        is_v2=False,
        single_ses=VALID_SES,
        single_result={"unrouted": 3},
    )
    assert report["status"] == "partial"
    assert "3" in report["message"]


def test_partial_ladder_winner_reports_partial(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: VALID_SES},
        run_results={50: {"unrouted": 5, "vias": 2}},
        via_cost_ladder=[50],
    )
    assert report["status"] == "partial"
    assert report["via_ladder"]["complete"] is False
    assert "5" in report["message"]


def test_success_path_cleans_up_artifacts(unrouted_board, monkeypatch):
    report = _autoroute(
        unrouted_board, monkeypatch,
        ses_by_via_cost={50: VALID_SES},
        via_cost_ladder=[50],
    )
    assert report["status"] == "success"
    assert not (unrouted_board.parent / "freerouting_vc50.ses").exists()
    assert not (unrouted_board.parent / "freerouting.dsn").exists()


# ---------------------------------------------------------------------------
# Z6 (spec-zones, F-ECC2) — refill zones between SES import and post-route DRC
# ---------------------------------------------------------------------------

BOARD_WITH_ZONE = BOARD_WITH_ROUTES.replace(
    "  (via (at 15 15) (size 0.8) (drill 0.4) (layers \"F.Cu\" \"B.Cu\") (net 1))",
    "  (via (at 15 15) (size 0.8) (drill 0.4) (layers \"F.Cu\" \"B.Cu\") (net 1))\n"
    "  (zone (net 1) (net_name \"GND\") (layer \"B.Cu\") (uuid \"z1\")\n"
    "    (name \"gnd_pour\")\n"
    "    (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40)))\n"
    "  )",
)


@pytest.fixture
def zoned_board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(BOARD_WITH_ZONE, encoding="utf-8")
    return f


def test_autoroute_refills_before_drc(zoned_board, monkeypatch):
    calls: list[Path] = []

    def fake_refill(p, backend):
        calls.append(p)
        return {"status": "success", "via": "file", "zones_filled": 1}

    monkeypatch.setattr("kicad_mcp.tools.board._impl_refill_zones", fake_refill)
    report = _autoroute(zoned_board, monkeypatch,
                        ses_by_via_cost={50: VALID_SES})

    assert calls == [zoned_board]
    steps = [s["step"] for s in report["steps"]]
    assert "zone_refill" in steps and "post_route_drc" in steps
    assert steps.index("zone_refill") < steps.index("post_route_drc")
    refill = _step(report, "zone_refill")
    assert refill["status"] == "ok" and refill["zones_filled"] == 1
    drc = _step(report, "post_route_drc")
    assert "stale" not in drc.get("message", "")


def test_autoroute_refill_failure_notes_stale(zoned_board, monkeypatch):
    monkeypatch.setattr(
        "kicad_mcp.tools.board._impl_refill_zones",
        lambda p, backend: {
            "status": "unavailable", "error_code": "zone_refill_unavailable",
            "reason": "KiCad closed and no bundled interpreter found: test",
        },
    )
    report = _autoroute(zoned_board, monkeypatch,
                        ses_by_via_cost={50: VALID_SES})

    refill = _step(report, "zone_refill")
    assert refill["status"] == "skipped"
    assert "no bundled interpreter" in refill["message"]
    drc = _step(report, "post_route_drc")
    assert "zone fills may be stale; re-run run_drc after refill_zones" \
        in drc["message"]


def test_autoroute_zoneless_no_refill_step(routed_board, monkeypatch):
    def boom(p, backend):
        raise AssertionError("zoneless board must not trigger a refill")

    monkeypatch.setattr("kicad_mcp.tools.board._impl_refill_zones", boom)
    report = _autoroute(routed_board, monkeypatch,
                        ses_by_via_cost={50: VALID_SES})

    assert _step(report, "zone_refill") is None
    assert report["status"] in ("success", "success_with_drc_errors")
