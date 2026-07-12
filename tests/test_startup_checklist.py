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
      ipc_reachable  (bool, default False) — IPC API server answers ping
      ipc_board      (str|None, default None) — board name over IPC; "" means a
                     document exists but is still loading; None means no board
      ipc_enabled    (bool, default True)  — KICAD_MCP_IPC_ENABLED state
      recent_launch  (bool, default False) — #20/R1: server just launched pcbnew
    """
    kicad_running = overrides.get("kicad_running", True)
    bridge_pong = overrides.get("bridge_pong", True)
    bridge_error = overrides.get("bridge_error", None)
    active_board = overrides.get("active_board", "/board.kicad_pcb")
    cli_path = overrides.get("cli_path", "/usr/bin/kicad-cli")
    ipc_reachable = overrides.get("ipc_reachable", False)
    ipc_board = overrides.get("ipc_board", None)
    ipc_on = overrides.get("ipc_enabled", True)
    recent_launch = overrides.get("recent_launch", False)

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

    class FakeIPCConn:
        def ping(self):
            return ipc_reachable

        def board(self):
            if ipc_board is None:
                from kicad_mcp.backends.ipc_connection import IPCUnavailableError
                raise IPCUnavailableError("no PCB document is open")
            import types
            return types.SimpleNamespace(name=ipc_board)

    with patch("kicad_mcp.utils.platform_helper.is_kicad_running", return_value=kicad_running), \
         patch("kicad_mcp.utils.platform_helper.find_kicad_cli",
               return_value=(MagicMock(__str__=lambda s: cli_path) if cli_path else None)), \
         patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call), \
         patch("kicad_mcp.backends.plugin_backend._get_port", return_value=9760), \
         patch("kicad_mcp.backends.plugin_backend._get_ping_timeout", return_value=2.0), \
         patch("kicad_mcp.backends.ipc_connection.IPCConnection", return_value=FakeIPCConn()), \
         patch("kicad_mcp.backends.ipc_connection.ipc_enabled", return_value=ipc_on), \
         patch("kicad_mcp.utils.startup_state.recent_launch", return_value=recent_launch), \
         patch("kicad_mcp.backends.ipc_connection.connection_remedy",
               return_value="Enable the IPC API server."):
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


# ---------------------------------------------------------------------------
# _check_kicad_process — broadened to detect pcbnew/eeschema standalone editors
# ---------------------------------------------------------------------------

def test_check_kicad_process_detects_pcbnew_only(monkeypatch):
    """Standalone PCB editor (pcbnew.exe) should count as 'KiCad running'.

    Previously _check_kicad_process only matched kicad.exe (project manager),
    producing a false FAIL when the user had only the PCB editor open.
    """
    import subprocess as sp
    from kicad_mcp.utils import platform_helper

    # Pretend we're on Windows so the kicad.exe / pcbnew.exe / eeschema.exe path runs
    monkeypatch.setattr(platform_helper, "get_platform", lambda: "windows")
    # Avoid a CREATE_NO_WINDOW AttributeError on non-Windows test runners
    monkeypatch.setattr(sp, "CREATE_NO_WINDOW", 0, raising=False)

    def fake_run(cmd, *a, **kw):
        # tasklist /FI IMAGENAME eq <name> /NH
        name = cmd[2].split("eq ", 1)[1].strip()
        if name == "pcbnew.exe":
            return sp.CompletedProcess(cmd, 0, stdout="pcbnew.exe   1234 Console\n", stderr="")
        # All other names: empty output, simulating not running
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert platform_helper._check_kicad_process() is True


def test_check_kicad_process_returns_false_when_no_kicad_processes(monkeypatch):
    import subprocess as sp
    from kicad_mcp.utils import platform_helper

    monkeypatch.setattr(platform_helper, "get_platform", lambda: "windows")
    monkeypatch.setattr(sp, "CREATE_NO_WINDOW", 0, raising=False)

    def fake_run(cmd, *a, **kw):
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert platform_helper._check_kicad_process() is False


def test_bridge_not_reachable():
    result = _run(bridge_error=ConnectionRefusedError("refused"))
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["bridge_reachable"] == "FAIL"
    assert any("bridge" in action.lower() or "PCB editor" in action
               for action in result["required_actions"])


# ---------------------------------------------------------------------------
# REQ-GATE-1 — IPC-aware gate: one live path + a READY board is enough
# ---------------------------------------------------------------------------

def test_ipc_serves_board_with_bridge_down_is_ready():
    """#14/#20 cure: bridge blindness is non-fatal when IPC serves the board."""
    result = _run(bridge_error=ConnectionRefusedError("refused"),
                  ipc_reachable=True, ipc_board="my_board.kicad_pcb")
    assert result["ready_for_pcb"] is True
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["ipc_server_reachable"] == "PASS"
    assert statuses["bridge_reachable"] == "WARN"  # non-blocking
    assert statuses["bridge_version_ok"] == "WARN"
    assert statuses["pcb_editor_open"] == "PASS"
    assert statuses["project_loaded"] == "PASS"
    assert result["required_actions"] == []


def test_ipc_down_is_warn_when_bridge_serves():
    result = _run(ipc_reachable=False)
    assert result["ready_for_pcb"] is True
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["ipc_server_reachable"] == "WARN"
    assert statuses["bridge_reachable"] == "PASS"


def test_no_live_path_fails_with_combined_remedy():
    result = _run(bridge_error=ConnectionRefusedError("refused"),
                  ipc_reachable=False)
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["bridge_reachable"] == "FAIL"
    assert any("IPC API server" in action for action in result["required_actions"])


def test_ipc_board_still_loading_blocks_with_wait_guidance():
    """REQ-GATE-3: still-loading is distinguished from no-board; callers wait."""
    result = _run(bridge_error=ConnectionRefusedError("refused"),
                  ipc_reachable=True, ipc_board="")
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["pcb_editor_open"] == "FAIL"
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert "still loading" in details["pcb_editor_open"]
    assert any("Wait for the board" in action for action in result["required_actions"])


def test_ipc_reachable_no_board_falls_to_bridge_board():
    result = _run(ipc_reachable=True, ipc_board=None,
                  active_board="/bridge_board.kicad_pcb")
    assert result["ready_for_pcb"] is True
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["ipc_server_reachable"] == "PASS"
    assert statuses["pcb_editor_open"] == "PASS"
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert "bridge_board" in details["pcb_editor_open"]


def test_ipc_disabled_by_env_reported():
    result = _run(ipc_enabled=False)
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert statuses["ipc_server_reachable"] == "WARN"
    assert "KICAD_MCP_IPC_ENABLED" in details["ipc_server_reachable"]


def test_no_board_loaded():
    result = _run(active_board="")
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    assert statuses["pcb_editor_open"] == "FAIL"
    assert statuses["project_loaded"] == "FAIL"


# ---------------------------------------------------------------------------
# #20/R1 — bridge-only path distinguishes "still loading" from "no board"
# ---------------------------------------------------------------------------

def test_bridge_only_recent_launch_reports_still_loading():
    """IPC down + bridge up + empty board, but the server just launched pcbnew:
    report 'still loading — wait' (parity with open_kicad's pending), not
    'no board'."""
    result = _run(ipc_reachable=False, active_board="", recent_launch=True)
    assert result["ready_for_pcb"] is False
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert statuses["pcb_editor_open"] == "FAIL"
    assert "still loading" in details["pcb_editor_open"]
    assert any("Wait for the board" in action for action in result["required_actions"])


def test_bridge_only_no_recent_launch_reports_no_board():
    """Same path but no recent launch: keep the existing 'no board' wording +
    open-a-board action (the documented R1 limitation)."""
    result = _run(ipc_reachable=False, active_board="", recent_launch=False)
    assert result["ready_for_pcb"] is False
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert "No board loaded" in details["pcb_editor_open"]
    assert any("Open a .kicad_pcb" in action for action in result["required_actions"])


def test_recent_launch_does_not_override_a_ready_board():
    """A recent launch must not mask a genuinely-loaded board."""
    result = _run(ipc_reachable=False, active_board="/board.kicad_pcb",
                  recent_launch=True)
    statuses = {item["item"]: item["status"] for item in result["checklist"]}
    details = {item["item"]: item["detail"] for item in result["checklist"]}
    assert statuses["pcb_editor_open"] == "PASS"
    assert "still loading" not in details["pcb_editor_open"]


# ---------------------------------------------------------------------------
# Checklist structure
# ---------------------------------------------------------------------------

def test_checklist_contains_seven_items():
    result = _run()
    assert len(result["checklist"]) == 7
    items = [entry["item"] for entry in result["checklist"]]
    assert items == ["kicad_running", "ipc_server_reachable", "bridge_reachable",
                     "bridge_version_ok", "pcb_editor_open",
                     "kicad_cli_available", "project_loaded"]


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
