"""Tests for #7 — import_ses must not revert footprint Values.

Two complementary guards:

1. ``_import_ses_with_fallback`` (routing) — when the live bridge is stale,
   boardless, or unreachable, fall back to ``_import_ses_subprocess``, which
   loads the board FRESH from disk so file-written Values survive. This is the
   structural fix: a stale in-memory bridge board can no longer clobber the file.
2. ``_restore_footprint_values`` (bridge) — defensive snapshot/restore so
   ImportSpecctraSES can never change a Value even on the bridge path.

The subprocess import itself runs pcbnew and is verified live; here we mock it.
Mirrors tests/test_export_dsn_fallback.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.plugin_backend import (
    BridgeTemporarilyUnavailableError,
    StaleBoardError,
)
from kicad_mcp.tools.routing import _import_ses_with_fallback


# ---------------------------------------------------------------------------
# _import_ses_with_fallback — routing logic
# ---------------------------------------------------------------------------

def _backend(import_side):
    backend = MagicMock()
    if isinstance(import_side, Exception):
        backend.import_ses.side_effect = import_side
    else:
        backend.import_ses.return_value = import_side
    return backend


def test_uses_bridge_when_it_succeeds(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    ses = tmp_path / "b.ses"
    backend = _backend({"success": True, "new_tracks": 5})

    with patch("kicad_mcp.tools.routing._import_ses_subprocess") as sub:
        result = _import_ses_with_fallback(backend, p, ses)

    sub.assert_not_called()
    assert result == {"success": True, "new_tracks": 5}


def test_falls_back_when_bridge_stale(tmp_path: Path):
    """The #7 core: a stale bridge board must NOT save its in-memory Values over
    disk — route to the subprocess that loads fresh from disk instead."""
    p = tmp_path / "b.kicad_pcb"
    ses = tmp_path / "b.ses"
    backend = _backend(StaleBoardError("disk newer than loaded", 200.0, 100.0))

    with patch(
        "kicad_mcp.tools.routing._import_ses_subprocess",
        return_value={"success": True, "via": "subprocess"},
    ) as sub:
        result = _import_ses_with_fallback(backend, p, ses)

    sub.assert_called_once_with(p, ses)
    assert result["via"] == "subprocess"


def test_falls_back_when_bridge_unreachable(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    ses = tmp_path / "b.ses"
    backend = _backend(BridgeTemporarilyUnavailableError("bridge down"))

    with patch(
        "kicad_mcp.tools.routing._import_ses_subprocess",
        return_value={"success": True, "via": "subprocess"},
    ) as sub:
        result = _import_ses_with_fallback(backend, p, ses)

    sub.assert_called_once_with(p, ses)
    assert result["via"] == "subprocess"


def test_falls_back_when_bridge_boardless(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    ses = tmp_path / "b.ses"
    backend = _backend(
        RuntimeError(
            "Requested board 'D:/p/b.kicad_pcb' does not match open board ''."
        )
    )

    with patch(
        "kicad_mcp.tools.routing._import_ses_subprocess",
        return_value={"success": True, "via": "subprocess"},
    ) as sub:
        result = _import_ses_with_fallback(backend, p, ses)

    sub.assert_called_once_with(p, ses)
    assert result["via"] == "subprocess"


def test_reraises_unrelated_bridge_error(tmp_path: Path):
    p = tmp_path / "b.kicad_pcb"
    ses = tmp_path / "b.ses"
    backend = _backend(RuntimeError("ImportSpecctraSES failed for b.ses"))

    with patch("kicad_mcp.tools.routing._import_ses_subprocess") as sub:
        with pytest.raises(RuntimeError, match="ImportSpecctraSES failed"):
            _import_ses_with_fallback(backend, p, ses)
    sub.assert_not_called()


# ---------------------------------------------------------------------------
# _restore_footprint_values — bridge defensive snapshot/restore (#7 insurance)
# ---------------------------------------------------------------------------

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location(
        "kicad_mcp_bridge_import_ses_under_test", BRIDGE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeFp:
    def __init__(self, ref: str, value: str):
        self._ref = ref
        self._value = value

    def GetReference(self):
        return self._ref

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._value = value


def test_snapshot_then_restore_reverts_a_changed_value(bridge):
    fps = [_FakeFp("U6", "Adafruit_PMSA003I"), _FakeFp("R1", "10k")]
    before = bridge._snapshot_footprint_values(fps)

    # Simulate ImportSpecctraSES clobbering U6's Value with the footprint name.
    fps[0].SetValue("PMSA003I")

    restored = bridge._restore_footprint_values(fps, before)

    assert restored == 1
    assert fps[0].GetValue() == "Adafruit_PMSA003I"  # custom Value survives (#7)
    assert fps[1].GetValue() == "10k"


def test_restore_is_noop_when_nothing_changed(bridge):
    fps = [_FakeFp("U6", "Adafruit_PMSA003I"), _FakeFp("R1", "10k")]
    before = bridge._snapshot_footprint_values(fps)

    restored = bridge._restore_footprint_values(fps, before)

    assert restored == 0
    assert fps[0].GetValue() == "Adafruit_PMSA003I"


def test_restore_ignores_footprints_added_after_snapshot(bridge):
    fps = [_FakeFp("U6", "Adafruit_PMSA003I")]
    before = bridge._snapshot_footprint_values(fps)
    # A reference not in the snapshot is left untouched (no false restore).
    fps.append(_FakeFp("R9", "1k"))

    restored = bridge._restore_footprint_values(fps, before)

    assert restored == 0
    assert fps[1].GetValue() == "1k"
