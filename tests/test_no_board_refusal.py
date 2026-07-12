"""Tests for the #20 structured ``no_board`` refusal (R2).

Three layers, mirroring the stale_board tests:
- bridge ``_get_open_board`` raises the typed ``_NoBoardError`` when
  ``pcbnew.GetBoard()`` is None (fake pcbnew injected — no KiCad needed);
- bridge ``_dispatch_request`` turns it into a structured
  ``{error_code: "no_board"}`` response;
- client ``_tcp_call`` parses that back into a typed ``NoBoardError`` that
  propagates cleanly through ``PluginBoardOps._call`` to the caller (no
  self-heal retry, no disconnect, no file fallback).

The pcbnew-side refusal is also verified live; here we mock at the boundaries.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import kicad_plugin.kicad_mcp_bridge as bridge
from kicad_mcp.backends.plugin_backend import (
    NoBoardError,
    PluginBoardOps,
    StaleBoardError,
    _tcp_call,
)


# ---------------------------------------------------------------------------
# Bridge: _get_open_board raises _NoBoardError when no board is open
# ---------------------------------------------------------------------------

def _fake_pcbnew(board):
    mod = types.ModuleType("pcbnew")
    mod.GetBoard = lambda: board  # type: ignore[attr-defined]
    return mod


def test_get_open_board_raises_no_board_error_when_none():
    with patch.dict(sys.modules, {"pcbnew": _fake_pcbnew(None)}):
        with pytest.raises(bridge._NoBoardError):
            bridge._get_open_board("/tmp/whatever.kicad_pcb")


def test_get_open_board_returns_board_when_path_matches(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    class _Board:
        def GetFileName(self):
            return str(pcb)

    with patch.dict(sys.modules, {"pcbnew": _fake_pcbnew(_Board())}):
        got = bridge._get_open_board(str(pcb))
    assert isinstance(got, _Board)


# ---------------------------------------------------------------------------
# Bridge: _dispatch_request emits a structured no_board error_code
# ---------------------------------------------------------------------------

def test_dispatch_emits_no_board_error_code():
    def _raiser(_req):
        raise bridge._NoBoardError("No board is currently open in KiCad")

    with patch.dict(bridge._DISPATCH, {"_probe_no_board": _raiser}, clear=False):
        resp = bridge._dispatch_request({"method": "_probe_no_board"})
    assert resp["status"] == "error"
    assert resp["error_code"] == "no_board"
    assert "No board" in resp["message"]


def test_dispatch_generic_error_has_no_error_code():
    def _raiser(_req):
        raise ValueError("something unrelated broke")

    with patch.dict(bridge._DISPATCH, {"_probe_generic": _raiser}, clear=False):
        resp = bridge._dispatch_request({"method": "_probe_generic"})
    assert resp["status"] == "error"
    assert "error_code" not in resp


# ---------------------------------------------------------------------------
# Client: _tcp_call maps error_code:"no_board" → NoBoardError
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


def test_tcp_call_raises_no_board_error():
    response = {
        "status": "error",
        "error_code": "no_board",
        "message": "No board is currently open in KiCad",
    }
    with _patch_socket(response), pytest.raises(NoBoardError) as exc:
        _tcp_call("move_component", 1.0, path="/tmp/b.kicad_pcb")
    assert "No board" in str(exc.value)
    # A no_board is not a stale_board.
    assert not isinstance(exc.value, StaleBoardError)


def test_no_board_error_is_runtimeerror_not_backend_unavailable():
    from kicad_mcp.models.errors import BackendNotAvailableError

    assert issubclass(NoBoardError, RuntimeError)
    assert not issubclass(NoBoardError, BackendNotAvailableError)


# ---------------------------------------------------------------------------
# Client: a mutating op surfaces NoBoardError cleanly (no retry / no disconnect)
# ---------------------------------------------------------------------------

def test_mutating_op_propagates_no_board_without_retry_or_disconnect():
    calls: list[str] = []
    seen = {"disconnect": False}

    def fake_tcp_call(method: str, timeout: float, **kwargs):
        calls.append(method)
        raise NoBoardError("No board is currently open in KiCad")

    ops = PluginBoardOps()
    ops._on_disconnect = lambda: seen.__setitem__("disconnect", True)
    with patch("kicad_mcp.backends.plugin_backend._tcp_call", side_effect=fake_tcp_call):
        with pytest.raises(NoBoardError):
            ops.move_component(Path("/tmp/b.kicad_pcb"), "R1", 10.0, 10.0)

    # Exactly one attempt: no reload_board self-heal, no second call.
    assert calls == ["move_component"]
    # NoBoardError must NOT be treated as a bridge disconnect.
    assert seen["disconnect"] is False
