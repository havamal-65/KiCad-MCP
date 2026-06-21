"""Unit tests for the bridge's #14C disk/in-memory coherence helpers.

The bridge module runs inside KiCad's embedded Python and cannot import the
kicad_mcp package, so we load it via importlib (the pcbnew import fails
gracefully in CI, so the TCP server never binds) and exercise the pure-Python
coherence helpers, which use only os.path:

- _note_first_contact / _record_load_mtime — baseline bookkeeping
- _check_disk_coherence            — refuse a mutation when disk is newer
- _dispatch_request                — structured stale_board response shape

The pcbnew-side handlers themselves are verified in the live batch, not here.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location(
        "kicad_mcp_bridge_coherence_under_test", BRIDGE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_mtimes(bridge):
    """Each test starts with an empty coherence baseline map."""
    bridge._board_load_mtimes.clear()
    yield
    bridge._board_load_mtimes.clear()


def _board(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text("(kicad_pcb)", encoding="utf-8")
    return p


def _bump_mtime(p: Path, seconds: float) -> float:
    """Advance the file's mtime by *seconds* and return the new value."""
    new = os.path.getmtime(p) + seconds
    os.utime(p, (new, new))
    return new


# ---------------------------------------------------------------------------
# _note_first_contact — records once, never overwrites
# ---------------------------------------------------------------------------

def test_note_first_contact_records_baseline(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    key = bridge._norm_board_path(str(p))
    assert bridge._board_load_mtimes[key] == pytest.approx(os.path.getmtime(p))


def test_note_first_contact_does_not_overwrite(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    key = bridge._norm_board_path(str(p))
    original = bridge._board_load_mtimes[key]
    _bump_mtime(p, 100.0)
    bridge._note_first_contact(str(p))  # second contact must not move the baseline
    assert bridge._board_load_mtimes[key] == original


def test_note_first_contact_ignores_empty(bridge):
    bridge._note_first_contact("")
    assert bridge._board_load_mtimes == {}


# ---------------------------------------------------------------------------
# _record_load_mtime — updates the baseline to current disk state
# ---------------------------------------------------------------------------

def test_record_load_mtime_updates_baseline(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    new = _bump_mtime(p, 50.0)
    bridge._record_load_mtime(str(p))  # e.g. after a save / reload
    key = bridge._norm_board_path(str(p))
    assert bridge._board_load_mtimes[key] == pytest.approx(new)


# ---------------------------------------------------------------------------
# _check_disk_coherence — the refusal gate
# ---------------------------------------------------------------------------

def test_check_coherence_first_contact_records_and_passes(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._check_disk_coherence(str(p))  # no baseline yet → record + pass
    key = bridge._norm_board_path(str(p))
    assert key in bridge._board_load_mtimes


def test_check_coherence_passes_when_unchanged(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    bridge._check_disk_coherence(str(p))  # must not raise


def test_check_coherence_raises_when_disk_newer(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    _bump_mtime(p, 10.0)
    with pytest.raises(bridge._StaleBoardError) as exc:
        bridge._check_disk_coherence(str(p))
    assert exc.value.disk_mtime > exc.value.loaded_mtime


def test_check_coherence_passes_when_file_missing(bridge, tmp_path: Path):
    missing = tmp_path / "gone.kicad_pcb"
    bridge._check_disk_coherence(str(missing))  # unreadable file → pass, no raise


def test_check_coherence_ignores_empty(bridge):
    bridge._check_disk_coherence("")  # no path → pass


# ---------------------------------------------------------------------------
# _dispatch_request — structured stale_board response for mutating methods
# ---------------------------------------------------------------------------

def test_dispatch_unknown_method(bridge):
    resp = bridge._dispatch_request({"method": "does_not_exist"})
    assert resp["status"] == "error"
    assert "Unknown method" in resp["message"]


def test_dispatch_mutating_method_refuses_stale_board(bridge, tmp_path: Path):
    p = _board(tmp_path)
    # Seed an out-of-date baseline, then make disk newer.
    bridge._note_first_contact(str(p))
    _bump_mtime(p, 30.0)
    # clear_routes is a mutating method — the coherence pre-check fires before
    # the (pcbnew-dependent) handler ever runs.
    resp = bridge._dispatch_request({"method": "clear_routes", "path": str(p)})
    assert resp["status"] == "error"
    assert resp["error_code"] == "stale_board"
    assert resp["disk_mtime"] > resp["loaded_mtime"]


def test_dispatch_nonmutating_method_skips_coherence(bridge, tmp_path: Path):
    p = _board(tmp_path)
    bridge._note_first_contact(str(p))
    _bump_mtime(p, 30.0)
    # ping is not mutating: no coherence check, so a newer disk does not produce
    # a stale_board verdict (ping ignores the path entirely).
    resp = bridge._dispatch_request({"method": "ping", "path": str(p)})
    assert resp["status"] == "ok"
    assert resp["result"]["pong"] is True
