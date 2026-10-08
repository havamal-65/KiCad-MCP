"""Unit tests for launcher.dashboard.build_state (webview data layer)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from launcher import dashboard


@dataclass
class _Item:
    name: str
    path: Path


PROJECTS = [_Item("alpha", Path("C:/p/alpha.kicad_pcb")), _Item("beta", Path("C:/p/beta.kicad_pcb"))]


def _snap(**over):
    base = {
        "server": {"running": True, "pid": 4321, "uptime": 3661},
        "bridge": {"state": "HEALTHY", "detail": "ok"},
        "board": {"open_board": "alpha", "status": "bridge", "bridge_access": "ok"},
        "checklist": {
            "available": True,
            "checks": [
                {"item": "kicad_running", "status": "PASS"},
                {"item": "bridge_reachable", "status": "PASS"},
                {"item": "bridge_version_ok", "status": "PASS"},
                {"item": "pcb_editor_open", "status": "PASS"},
                {"item": "kicad_cli_available", "status": "PASS"},
                {"item": "project_loaded", "status": "PASS"},
            ],
        },
        "backends": {
            "rows": [
                {"name": "plugin (bridge)", "available": True},
                {"name": "file", "available": True},
                {"name": "cli", "available": True},
                {"name": "subprocess", "available": None},
            ]
        },
        "activity": {"recent": [{"tool": "run_drc", "status": "ok", "epoch": 0, "ts": "2026-07-13T09:15:30"}]},
        "errors": {"events": [{"ts": "2026-07-13T09:00:00", "level": "WARNING", "msg": "hi"}]},
    }
    base.update(over)
    return base


def test_loading_state_when_no_snapshot():
    st = dashboard.build_state(None, PROJECTS, 0)
    assert st["loading"] is True
    assert [p["label"] for p in st["projects"]] == ["alpha", "beta"]
    assert st["phase"] == "idle"


def test_all_green_running():
    st = dashboard.build_state(_snap(), PROJECTS, 0)
    assert st["loading"] is False
    assert st["server"] == {"status": "running", "pid": 4321, "uptimeSeconds": 3661}
    assert st["bridge"]["status"] == "running"
    assert "TCP :9760" in st["bridge"]["detail"]
    assert st["bridge"]["board"] == "alpha"
    assert all(st["checklist"].values())
    assert st["phase"] == "running"
    assert st["backends"] == {"plugin": "up", "file": "up", "cli": "up", "subprocess": "idle"}


def test_checklist_order_and_partial():
    snap = _snap(checklist={
        "available": True,
        "checks": [
            {"item": "kicad_running", "status": "PASS"},
            {"item": "kicad_cli_available", "status": "PASS"},
        ],
    })
    st = dashboard.build_state(snap, PROJECTS, 0)
    assert list(st["checklist"].keys()) == dashboard.CHECK_ORDER
    assert st["checklist"]["kicad_running"] is True
    assert st["checklist"]["project_loaded"] is False
    assert not all(st["checklist"].values())
    # server up but not fully ready -> idle
    assert st["phase"] == "idle"


def test_server_stopped_phase():
    # Server down, bridge also down (fully stopped scenario). Note the bridge is
    # independent of the MCP server, so a HEALTHY bridge with a stopped server is
    # a legitimate combo handled by test_bridge_down_maps_unresponsive.
    st = dashboard.build_state(
        _snap(server={"running": False}, bridge={"state": "DOWN", "detail": "refused"}),
        PROJECTS,
        0,
    )
    assert st["server"]["status"] == "stopped"
    assert st["phase"] == "stopped"
    assert st["bridge"]["status"] == "unresponsive"


def test_bridge_down_maps_unresponsive():
    st = dashboard.build_state(
        _snap(bridge={"state": "DOWN", "detail": "port 9760 refused"}, board={"open_board": None}),
        PROJECTS,
        0,
    )
    assert st["bridge"]["status"] == "unresponsive"
    assert st["bridge"]["detail"] == "port 9760 refused"
    assert st["bridge"]["board"] == "none open"
    assert st["boardOpen"] is False


def test_busy_phase_forces_starting():
    st = dashboard.build_state(_snap(server={"running": False}), PROJECTS, 0, busy_phase="starting")
    assert st["phase"] == "starting"
    assert st["bridge"]["status"] == "starting"


def test_activity_and_logs_shape():
    st = dashboard.build_state(_snap(), PROJECTS, 0)
    assert st["activity"][0]["tool"] == "run_drc"
    assert st["activity"][0]["status"] == "ok"
    assert len(st["activity"][0]["time"]) == 8
    assert st["logs"][0]["level"] == "WARNING"


def test_healthy_but_impaired_access_is_starting():
    st = dashboard.build_state(
        _snap(board={"open_board": "alpha", "status": "file", "bridge_access": "broken"}),
        PROJECTS,
        0,
    )
    assert st["bridge"]["status"] == "starting"


def test_selected_index_out_of_range_clamps():
    st = dashboard.build_state(_snap(), PROJECTS, 99)
    assert st["projectIndex"] == 0
    assert st["projectPath"].endswith("alpha.kicad_pcb")


# --- board_format_hint (U5 VS-5) ---------------------------------------------

_PCB_HEAD = '(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n\t(generator_version "{v}")\n'


def _board(tmp_path, generator_version):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_PCB_HEAD.format(v=generator_version), encoding="utf-8")
    return p


def test_hint_newer_board_older_kicad(tmp_path):
    hint = dashboard.board_format_hint(_board(tmp_path, "10.0"), "9.0")
    assert hint is not None
    assert "b.kicad_pcb" in hint and "KiCad 10" in hint and "9.0" in hint


def test_hint_none_when_majors_equal(tmp_path):
    assert dashboard.board_format_hint(_board(tmp_path, "10.0"), "10.0") is None


def test_hint_none_when_board_older(tmp_path):
    assert dashboard.board_format_hint(_board(tmp_path, "9.0"), "10.0") is None


def test_hint_tolerates_missing_inputs(tmp_path):
    assert dashboard.board_format_hint(None, "10.0") is None
    assert dashboard.board_format_hint(_board(tmp_path, "10.0"), None) is None
    assert dashboard.board_format_hint(_board(tmp_path, "10.0"), "") is None
    assert dashboard.board_format_hint(tmp_path / "ghost.kicad_pcb", "9.0") is None


def test_hint_tolerates_unstamped_or_garbage(tmp_path):
    p = tmp_path / "old.kicad_pcb"
    p.write_text("(kicad_pcb (version 20171130) (host pcbnew 4.0))\n", encoding="utf-8")
    assert dashboard.board_format_hint(p, "9.0") is None          # no stamp
    assert dashboard.board_format_hint(_board(tmp_path, "10.0"), "abc") is None
