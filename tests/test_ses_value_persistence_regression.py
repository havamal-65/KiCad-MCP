"""REQ-COV: footprint Value survives an SES routing import (#7 regression).

The canonical #7 catch. A footprint Value written to the .kicad_pcb (the
schematic-synced value, e.g. ``Adafruit_PMSA003I``) must SURVIVE a Specctra SES
import — the autoroute step that historically reverted it to the footprint name
because a stale in-memory bridge board was saved over the file.

``_import_ses_subprocess`` is the structural fix: it loads the board FRESH from
disk (so it carries the latest file-written Value) before importing routing and
saving. Reverting that fix — e.g. importing into a board that does not reflect
the on-disk Value — must make this test fail.

SES import requires real pcbnew, so this is gated on a pcbnew import and on a
checked-in board+ses fixture pair (generated from a real autoroute run in the
S6 live-verify session). It is NOT integration-marked: it runs in the default
suite on any machine with KiCad installed, including the live session, so the
catch-class fires outside the opt-in integration gate.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp.backends.subprocess_backend import _get_kicad_python, _get_pcbnew
from kicad_mcp.tools.routing import _import_ses_subprocess

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "boards"
_FIXTURE_BOARD = _FIXTURE_DIR / "ses_value_revert_v1.kicad_pcb"
_FIXTURE_SES = _FIXTURE_DIR / "ses_value_revert_v1.ses"
# The custom Value the fixture board carries on R1 and must keep after import.
_CUSTOM_VALUE = "Adafruit_PMSA003I"
_REF = "R1"

# _import_ses_subprocess runs pcbnew either in-process (if importable) or via
# KiCad's bundled Python (subprocess). Skip only when NEITHER is available.
_PCBNEW_AVAILABLE = _get_pcbnew() is not None or _get_kicad_python() is not None


pytestmark = [
    pytest.mark.skipif(not _PCBNEW_AVAILABLE, reason="no pcbnew (in-process or bundled) available"),
    pytest.mark.skipif(
        not (_FIXTURE_BOARD.is_file() and _FIXTURE_SES.is_file()),
        reason=(
            "SES regression fixtures missing — generated from a real autoroute "
            "run in the S6 live-verify session (see plan.md)."
        ),
    ),
]


def _value_of(board_path: Path, reference: str) -> str:
    """Read a footprint's Value straight from the .kicad_pcb text (no pcbnew, so
    the assertion works whether the import ran in-process or via subprocess)."""
    text = board_path.read_text(encoding="utf-8")
    for m in re.finditer(r"\(footprint\s", text):
        start = m.start()
        depth, i = 0, start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    break
            i += 1
        else:
            continue
        if re.search(rf'\(property\s+"Reference"\s+"{re.escape(reference)}"', block):
            vm = re.search(r'\(property\s+"Value"\s+"([^"]*)"', block)
            assert vm is not None, f"{reference} has no Value property"
            return vm.group(1)
    raise AssertionError(f"{reference} missing from {board_path}")


def test_ses_import_preserves_custom_value(tmp_path: Path):
    board = tmp_path / _FIXTURE_BOARD.name
    ses = tmp_path / _FIXTURE_SES.name
    shutil.copy2(_FIXTURE_BOARD, board)
    shutil.copy2(_FIXTURE_SES, ses)

    # Precondition: the fixture board carries the custom Value on disk.
    assert _value_of(board, _REF) == _CUSTOM_VALUE

    result = _import_ses_subprocess(board, ses)

    assert result["success"] is True
    assert result["via"] == "subprocess"
    # Routing was applied...
    assert result["new_tracks"] >= 1
    # ...and the custom Value SURVIVED (the #7 regression guard).
    assert _value_of(board, _REF) == _CUSTOM_VALUE, (
        f"{_REF}'s Value reverted after SES import — #7 has regressed. "
        "_import_ses_subprocess must load the board fresh from disk."
    )
