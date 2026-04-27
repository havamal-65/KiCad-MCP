"""Tests for the get_startup_checklist / run_startup_checklist logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(**overrides):
    """Call run_startup_checklist with controllable mocks.

    Keyword args override the default mock behaviour:
      kicad_running  (bool, default True)
      bridge_pong    (bool, default True)  — _tcp_call("ping") returns pong
      bridge_error   (Exception|None)       — if set, _tcp_call raises this
      active_board   (str, default "/board.kicad_pcb")
      cli_path       (str|None, default "/usr/bin/kicad-cli")
    """
    kicad_running = overrides.get("kicad_running", True)
    bridge_pong = overrides.get("bridge_pong", True)
    bridge_error = overrides.get("bridge_error", None)
    active_board = overrides.get("active_board", "/board.kicad_pcb")
    cli_path = overrides.get("cli_path", "/usr/bin/kicad-cli")

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        if bridge_error:
            raise bridge_error
        if method == "ping":
            return {"pong": bridge_pong}
        if method == "get_info":
            return {"version": "2.0.0"}
        if method == "get_active_project":
            return {"board_path": active_board}
        return {}

    with patch("kicad_mcp.utils.platform_helper.is_kicad_running", return_value=kicad_running), \
         patch("kicad_mcp.utils.platform_helper.find_kicad_cli",
               return_value=(MagicMock(__str__=lambda s: cli_path) if cli_path else None)), \
         patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call), \
         patch("kicad_mcp.backends.plugin_backend._get_port", return_value=9760), \
         patch("kicad_mcp.backends.plugin_backend._get_ping_timeout", return_value=2.0):
        from kicad_mcp.tools.project import run_startup_checklist
        return run_startup_checklist()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_all_pass_when_everything_ok():
    result = _run()
    assert result["ready_for_pcb"] is True
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["kicad_running"] == "PASS"
    assert statuses["bridge_reachable"] == "PASS"
    assert statuses["pcb_editor_open"] == "PASS"
    assert statuses["project_loaded"] == "PASS"
    # kicad_cli_available may be PASS or WARN depending on environment
    assert statuses["kicad_cli_available"] in ("PASS", "WARN")


def test_ready_for_pcb_is_true_with_warn():
    """WARN on kicad_cli should not block ready_for_pcb."""
    result = _run(cli_path=None)
    assert result["ready_for_pcb"] is True
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["kicad_cli_available"] == "WARN"


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------

def test_kicad_not_running():
    result = _run(kicad_running=False)
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["kicad_running"] == "FAIL"
    assert any("KiCad" in action for action in result["required_actions"])


def test_bridge_not_reachable():
    result = _run(bridge_error=ConnectionRefusedError("refused"))
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["bridge_reachable"] == "FAIL"
    assert any("bridge" in action.lower() or "PCB editor" in action
               for action in result["required_actions"])


def test_no_board_loaded():
    result = _run(active_board="")
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["pcb_editor_open"] == "FAIL"
    assert statuses["project_loaded"] == "FAIL"


# ---------------------------------------------------------------------------
# Checklist structure
# ---------------------------------------------------------------------------

def test_checklist_contains_six_items():
    result = _run()
    assert len(result["checklist"]) == 6


def test_checklist_item_keys():
    result = _run()
    for item in result["checklist"]:
        assert "item" in item
        assert "status" in item
        assert "detail" in item
        assert item["status"] in ("PASS", "FAIL", "WARN")


def test_required_actions_empty_when_all_pass():
    result = _run()
    # May have entries if kicad-cli is WARN, but required_actions only covers FAIL items
    # i.e. no FAIL items → no required_actions
    fail_items = [i for i in result["checklist"] if i["status"] == "FAIL"]
    if not fail_items:
        assert result["required_actions"] == []
