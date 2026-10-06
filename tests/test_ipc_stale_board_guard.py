"""F-DV11 (DV build) — IPC writes must refuse when the live board is stale.

KiCad never reloads a board in place: file-side tools (placement engine,
autoroute DSN/SES) rewrite the ``.kicad_pcb`` while the GUI's in-memory board
stays at its load-time state. An IPC mutation then commits + saves that stale
board OVER the newer file — live-observed 2026-10-05, when a zone heal saved
a pre-placement scatter board over the fully placed + routed recreation file.

The guard: every ``IPCBoardOps`` write path resolves the board with
``check_coherence=True``, which compares the live board against the file
(footprint set + positions, track/via/zone counts) and raises
``IPCStaleBoardError`` on any disagreement. The error is never retried on
the bridge — the bridge holds the SAME stale live board.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("kipy")

from kicad_mcp.backends.base import BoardOps
from kicad_mcp.backends.ipc_backend import IPCBoardOps, _coherence_mismatch
from kicad_mcp.backends.ipc_connection import IPCStaleBoardError, IPCUnavailableError
from kicad_mcp_plugin.backends.plugin_direct import _LiveBoardOps


# ── synthetic file + live-board doubles ─────────────────────────────────────

def _fp_block(ref: str, x: float, y: float, rot: float | None = None) -> str:
    at = f"(at {x} {y} {rot})" if rot is not None else f"(at {x} {y})"
    return (
        f'\t(footprint "Lib:FP"\n'
        f'\t\t(layer "F.Cu")\n'
        f"\t\t{at}\n"
        f'\t\t(property "Reference" "{ref}"\n'
        f"\t\t\t(at 0 0 0)\n"
        f"\t\t)\n"
        f"\t)\n"
    )


def _board_text(
    footprints: list[tuple[str, float, float]],
    tracks: int = 0,
    vias: int = 0,
    zones: int = 0,
) -> str:
    parts = ['(kicad_pcb\n\t(version 20241229)\n\t(generator "test")\n']
    for ref, x, y in footprints:
        parts.append(_fp_block(ref, x, y, rot=90.0 if ref == "C1" else None))
    parts.extend(
        '\t(segment (start 0 0) (end 1 0) (width 0.8) (layer "B.Cu") (net 1))\n'
        for _ in range(tracks)
    )
    parts.extend(
        '\t(via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        for _ in range(vias)
    )
    parts.extend(
        '\t(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z"))\n'
        for _ in range(zones)
    )
    parts.append(")\n")
    return "".join(parts)


class _TextVal:
    def __init__(self, value: str) -> None:
        self.value = value


class _RefField:
    def __init__(self, ref: str) -> None:
        self.text = _TextVal(ref)


class _Pos:
    def __init__(self, x_nm: int, y_nm: int) -> None:
        self.x = x_nm
        self.y = y_nm


class _LiveFP:
    def __init__(self, ref: str, x_mm: float, y_mm: float) -> None:
        self.reference_field = _RefField(ref)
        self.position = _Pos(round(x_mm * 1e6), round(y_mm * 1e6))


class _FakeLiveBoard:
    def __init__(
        self,
        name: str,
        footprints: list[tuple[str, float, float]],
        tracks: int = 0,
        vias: int = 0,
        zones: int = 0,
    ) -> None:
        self.name = name
        self._fps = [_LiveFP(ref, x, y) for ref, x, y in footprints]
        self._tracks = tracks
        self._vias = vias
        self._zones = zones

    def get_footprints(self) -> list[_LiveFP]:
        return self._fps

    def get_tracks(self) -> list[object]:
        return [object()] * self._tracks

    def get_vias(self) -> list[object]:
        return [object()] * self._vias

    def get_zones(self) -> list[object]:
        return [object()] * self._zones


_FPS = [("C1", 141.605, 99.695), ("R1", 136.271, 107.95), ("U1", 149.225, 113.665)]


def _write_board(tmp_path: Path, **kwargs: Any) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(_FPS, **kwargs), encoding="utf-8")
    return path


# ── _coherence_mismatch unit coverage ───────────────────────────────────────

def test_matching_board_and_file_is_coherent(tmp_path: Path) -> None:
    path = _write_board(tmp_path, tracks=2, vias=1, zones=1)
    board = _FakeLiveBoard("board.kicad_pcb", _FPS, tracks=2, vias=1, zones=1)
    assert _coherence_mismatch(board, path) is None


def test_moved_footprint_is_a_mismatch(tmp_path: Path) -> None:
    path = _write_board(tmp_path)
    moved = [("C1", 141.605, 99.695), ("R1", 50.0, 4.5), ("U1", 149.225, 113.665)]
    board = _FakeLiveBoard("board.kicad_pcb", moved)
    mismatch = _coherence_mismatch(board, path)
    assert mismatch is not None and "R1" in mismatch


def test_differing_footprint_sets_are_a_mismatch(tmp_path: Path) -> None:
    path = _write_board(tmp_path)
    board = _FakeLiveBoard("board.kicad_pcb", _FPS[:2])  # U1 missing live
    mismatch = _coherence_mismatch(board, path)
    assert mismatch is not None and "U1" in mismatch


def test_track_count_drift_is_a_mismatch(tmp_path: Path) -> None:
    path = _write_board(tmp_path, tracks=34)
    board = _FakeLiveBoard("board.kicad_pcb", _FPS, tracks=0)
    mismatch = _coherence_mismatch(board, path)
    assert mismatch is not None and "track count" in mismatch


def test_via_count_drift_is_a_mismatch(tmp_path: Path) -> None:
    path = _write_board(tmp_path, vias=2)
    board = _FakeLiveBoard("board.kicad_pcb", _FPS, vias=0)
    mismatch = _coherence_mismatch(board, path)
    assert mismatch is not None and "via count" in mismatch


def test_zone_count_drift_is_a_mismatch(tmp_path: Path) -> None:
    path = _write_board(tmp_path, zones=1)
    board = _FakeLiveBoard("board.kicad_pcb", _FPS, zones=0)
    mismatch = _coherence_mismatch(board, path)
    assert mismatch is not None and "zone count" in mismatch


def test_sub_tolerance_position_delta_is_coherent(tmp_path: Path) -> None:
    path = _write_board(tmp_path)
    nudged = [(ref, x + 0.0005, y) for ref, x, y in _FPS]  # 500 nm < 1 µm tol
    board = _FakeLiveBoard("board.kicad_pcb", nudged)
    assert _coherence_mismatch(board, path) is None


def test_missing_file_is_coherent_new_board(tmp_path: Path) -> None:
    board = _FakeLiveBoard("board.kicad_pcb", _FPS)
    assert _coherence_mismatch(board, tmp_path / "nonexistent.kicad_pcb") is None


# ── _board(check_coherence=True) raises the structured error ────────────────

class _StubConn:
    def __init__(self, board: _FakeLiveBoard) -> None:
        self._board = board

    def board(self) -> _FakeLiveBoard:
        return self._board


def _ipc_ops(board: _FakeLiveBoard) -> IPCBoardOps:
    return IPCBoardOps(_StubConn(board))  # type: ignore[arg-type]


def test_board_write_guard_raises_stale_board_error(tmp_path: Path) -> None:
    path = _write_board(tmp_path, tracks=34)
    stale = _FakeLiveBoard("board.kicad_pcb", _FPS, tracks=0)
    with pytest.raises(IPCStaleBoardError) as excinfo:
        _ipc_ops(stale)._board(path, check_coherence=True)
    assert "stale_board" in str(excinfo.value)
    assert excinfo.value.remedy is not None
    assert "reopen" in excinfo.value.remedy


def test_board_read_path_skips_the_guard(tmp_path: Path) -> None:
    path = _write_board(tmp_path, tracks=34)
    stale = _FakeLiveBoard("board.kicad_pcb", _FPS, tracks=0)
    # Default (read) resolution must still serve the stale board: reads are
    # safe and refusing them would break diagnosis of this exact state.
    assert _ipc_ops(stale)._board(path) is stale


def test_board_coherent_write_passes_the_guard(tmp_path: Path) -> None:
    path = _write_board(tmp_path, tracks=2, zones=1)
    live = _FakeLiveBoard("board.kicad_pcb", _FPS, tracks=2, zones=1)
    assert _ipc_ops(live)._board(path, check_coherence=True) is live


# ── _LiveBoardOps must NOT bridge-retry a stale-board refusal ───────────────

class _StubOps(BoardOps):
    def read_board(self, path: Path) -> dict[str, Any]:
        return {}

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        return []

    def get_board_info(self, path: Path) -> dict[str, Any]:
        return {}


class _StaleRaisingOps(_StubOps):
    def add_zone(  # type: ignore[override]
        self, path: Path, net: str, layer: str,
        outline: list[dict[str, float]], **kwargs: Any,
    ) -> dict[str, Any]:
        raise IPCStaleBoardError("Board write refused (stale_board)", remedy="reopen")


class _UnavailableOps(_StubOps):
    def add_zone(  # type: ignore[override]
        self, path: Path, net: str, layer: str,
        outline: list[dict[str, float]], **kwargs: Any,
    ) -> dict[str, Any]:
        raise IPCUnavailableError("server gone")


class _BridgeOps(_StubOps):
    def __init__(self) -> None:
        self.calls = 0

    def add_zone(  # type: ignore[override]
        self, path: Path, net: str, layer: str,
        outline: list[dict[str, float]], **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls += 1
        return {"status": "success", "served_by": "bridge"}


_OUTLINE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}]


def test_live_ops_reraises_stale_board_without_bridge_retry() -> None:
    bridge = _BridgeOps()
    ops = _LiveBoardOps(_StaleRaisingOps(), lambda: bridge, lambda path: None)
    with pytest.raises(IPCStaleBoardError):
        ops.add_zone(Path("board.kicad_pcb"), "GND", "B.Cu", _OUTLINE)
    assert bridge.calls == 0, "bridge holds the SAME stale board — never retry"


def test_live_ops_still_bridge_retries_plain_unavailable() -> None:
    # Contrast: the parent IPCUnavailableError stays a fallback trigger, so
    # the stale subclass must be caught (re-raised) BEFORE the fallback catch.
    bridge = _BridgeOps()
    ops = _LiveBoardOps(_UnavailableOps(), lambda: bridge, lambda path: None)
    result = ops.add_zone(Path("board.kicad_pcb"), "GND", "B.Cu", _OUTLINE)
    assert result == {"status": "success", "served_by": "bridge"}
    assert bridge.calls == 1
