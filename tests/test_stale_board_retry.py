"""Tests for the #14C client-side stale-board self-heal.

Two layers:
- _tcp_call parses a structured ``stale_board`` error response into a typed
  StaleBoardError (carrying the two mtimes), via a fake socket.
- PluginBoardOps._call reloads the board once and retries the original op once
  when it hits StaleBoardError; a second stale verdict propagates.

The pcbnew-side refusal is verified live; here we mock at the TCP boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    PluginBoardOps,
    StaleBoardError,
    _tcp_call,
)


# ---------------------------------------------------------------------------
# _tcp_call — parses the stale_board error response into StaleBoardError
# ---------------------------------------------------------------------------

class _FakeSock:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _t) -> None:
        pass

    def recv(self, n: int) -> bytes:
        chunk, self._payload = self._payload[:n], self._payload[n:]
        return chunk


def _patch_socket(response: dict):
    payload = (json.dumps(response) + "\n").encode("utf-8")
    return patch(
        "kicad_mcp.backends.plugin_backend.socket.create_connection",
        return_value=_FakeSock(payload),
    )


def test_tcp_call_raises_stale_board_error():
    response = {
        "status": "error",
        "error_code": "stale_board",
        "message": "Board on disk is newer than the bridge's in-memory copy",
        "disk_mtime": 200.0,
        "loaded_mtime": 100.0,
    }
    with _patch_socket(response), pytest.raises(StaleBoardError) as exc:
        _tcp_call("clear_routes", 1.0, path="/tmp/b.kicad_pcb")
    assert exc.value.disk_mtime == 200.0
    assert exc.value.loaded_mtime == 100.0


def test_tcp_call_generic_error_is_runtimeerror_not_stale():
    response = {"status": "error", "message": "something else broke"}
    with _patch_socket(response), pytest.raises(RuntimeError) as exc:
        _tcp_call("clear_routes", 1.0, path="/tmp/b.kicad_pcb")
    assert not isinstance(exc.value, StaleBoardError)


# ---------------------------------------------------------------------------
# PluginBoardOps._call — reload-once-then-retry-once state machine
# ---------------------------------------------------------------------------

def test_call_reloads_and_retries_on_stale_board():
    calls: list[tuple[str, dict]] = []

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        calls.append((method, kwargs))
        if method == "clear_routes" and len([c for c in calls if c[0] == "clear_routes"]) == 1:
            raise StaleBoardError("stale", 2.0, 1.0)  # first attempt only
        if method == "reload_board":
            return {"success": True, "loaded": True}  # board actually reloaded
        return {"status": "success", "tracks_removed": 4}

    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        result = PluginBoardOps().clear_routes(Path("/tmp/b.kicad_pcb"), backup=True)

    methods = [c[0] for c in calls]
    assert methods == ["clear_routes", "reload_board", "clear_routes"]
    # reload_board targeted the same board path
    assert calls[1][1]["path"] == str(Path("/tmp/b.kicad_pcb"))
    assert result == {"status": "success", "tracks_removed": 4}


def test_call_propagates_when_still_stale_after_reload():
    def fake_tcp_call(method: str, timeout: float, **kwargs):
        if method == "reload_board":
            return {"success": True, "loaded": True}
        raise StaleBoardError("still stale", 3.0, 1.0)  # clear_routes always stale

    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        with pytest.raises(StaleBoardError):
            PluginBoardOps().clear_routes(Path("/tmp/b.kicad_pcb"))


def test_call_refuses_without_retry_when_reload_could_not_load():
    """KiCad-9 case: board.Load() fails so reload_board reports loaded=False.

    The client must NOT retry the mutation (retrying would re-stale or clobber
    the newer disk). It surfaces StaleBoardError with GUI-revert guidance, and
    the original op is attempted exactly once (the first, failing, call).
    """
    calls: list[str] = []

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        calls.append(method)
        if method == "reload_board":
            return {"success": True, "loaded": False}  # pcbnew could not reload
        raise StaleBoardError("stale", 2.0, 1.0)

    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        with pytest.raises(StaleBoardError, match="Revert/reload the board"):
            PluginBoardOps().clear_routes(Path("/tmp/b.kicad_pcb"))

    # clear_routes attempted once, reload attempted once, NO second clear_routes.
    assert calls == ["clear_routes", "reload_board"]


def test_call_reload_failure_marks_bridge_down():
    seen = {"disconnect": False}

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        if method == "reload_board":
            raise BridgeTemporarilyUnavailableError("bridge vanished during reload")
        raise StaleBoardError("stale", 2.0, 1.0)

    ops = PluginBoardOps()
    ops._on_disconnect = lambda: seen.__setitem__("disconnect", True)
    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        with pytest.raises(BridgeTemporarilyUnavailableError):
            ops.clear_routes(Path("/tmp/b.kicad_pcb"))
    assert seen["disconnect"] is True
