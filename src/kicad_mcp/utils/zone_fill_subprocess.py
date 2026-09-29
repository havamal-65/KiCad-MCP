"""File-side zone fill via KiCad's own bundled Python (spec-zones Z4).

KiCad 9.0.7's ``ZONE_FILLER.Fill`` hard-crashes (0xC0000005) on a board
constructed in memory, but works on a ``LoadBoard``-attached board (measured
2026-08-21, spec-zones facts 2/3). So the generated one-shot script always
``LoadBoard``s the saved file — never builds a ``BOARD()`` — fills, and saves.
It counts zones first and skips Fill+Save entirely when there are none, so a
no-op fill never dirties the file (Z5 no-dirty rule).

Only ever invoked when KiCad is confirmed closed (callers hold the
``_refuse_if_open_in_kicad`` pass); never a substitute for the live paths.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.kicad_version import bin_dir, probe_order

logger = get_logger("utils.zone_fill_subprocess")

FILL_TIMEOUT = 120  # seconds

# The one-shot script prints exactly one JSON line to stdout; the parent
# parses that line and nothing else.
_FILL_SCRIPT = r"""
import json
import sys

import pcbnew

board = pcbnew.LoadBoard(sys.argv[1])
zones = board.Zones()
count = len(zones) if hasattr(zones, "__len__") else zones.GetCount()
if count > 0:
    pcbnew.ZONE_FILLER(board).Fill(zones)
    pcbnew.SaveBoard(sys.argv[1], board)
print(json.dumps({"zones_filled": count, "filled": count > 0}))
"""


_GENERATOR_VERSION_RE = re.compile(r'\(generator_version "(\d+)')
_VERSION_SEGMENT_RE = re.compile(r"\d+\.\d+")


def _board_format_major(pcb_path: Path) -> str | None:
    """The KiCad major that last saved *pcb_path* (``generator_version``
    header), or None when unreadable/absent (pre-7 files have no header)."""
    try:
        with pcb_path.open(encoding="utf-8", errors="ignore") as fh:
            head = fh.read(500)
    except OSError:
        return None
    m = _GENERATOR_VERSION_RE.search(head)
    return m.group(1) if m else None


def _interpreter_major(interpreter: Path) -> str | None:
    """The KiCad major a bundled interpreter belongs to, from its install
    path's ``<major>.<minor>`` segment (Windows layout)."""
    for part in interpreter.parts:
        if _VERSION_SEGMENT_RE.fullmatch(part):
            return part.split(".")[0]
    return None


def find_bundled_python(format_major: str | None = None) -> Path | None:
    """A bundled KiCad interpreter, or None.

    Iterates the K10-S1 version resolver's probe order and returns the first
    ``<install>/bin/python.exe`` that exists (Windows layout; other platforms
    have no bundled interpreter in this layout and resolve to None). When
    *format_major* is given, installs of that major are tried FIRST — a newer
    pcbnew's SaveBoard silently rewrites the file in its own newer format
    (measured: 10.0 fill upgraded a 9.0 board to version 20260206, which
    KiCad 9 can no longer open), so a format-matched interpreter preserves
    the board's format and a newer one is only a fallback.
    """
    order = probe_order()
    if format_major is not None:
        matched = [v for v in order if v.split(".")[0] == format_major]
        order = matched + [v for v in order if v not in matched]
    for version in order:
        candidate = bin_dir(version) / "python.exe"
        if candidate.exists():
            return candidate
    return None


def fill_zones_subprocess(pcb_path: Path) -> dict[str, Any]:
    """Fill every zone of a saved board out-of-process.

    Returns ``{"filled": bool, "zones_filled": N, "interpreter": path}`` on
    success (plus ``"warnings"`` when the fill had to run under a different
    KiCad major than the one that saved the board, which re-saves the file in
    that major's format) or ``{"filled": False, "reason": …}`` on any failure
    (missing interpreter, nonzero exit, bad JSON, timeout).
    """
    format_major = _board_format_major(pcb_path)
    interpreter = find_bundled_python(format_major)
    if interpreter is None:
        return {"filled": False, "reason": "no bundled KiCad python interpreter found"}

    try:
        result = subprocess.run(
            [str(interpreter), "-c", _FILL_SCRIPT, str(pcb_path)],
            capture_output=True,
            text=True,
            timeout=FILL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"filled": False,
                "reason": f"zone fill timed out after {FILL_TIMEOUT}s"}
    except OSError as exc:
        return {"filled": False, "reason": f"failed to run {interpreter}: {exc}"}

    if result.returncode != 0:
        logger.error("subprocess zone fill failed: %s", result.stderr)
        return {"filled": False,
                "reason": f"fill process exited {result.returncode}: "
                          f"{result.stderr.strip()[-500:]}"}

    # Exactly one JSON line on stdout is the contract; scan for it in case
    # pcbnew emits import-time noise around it.
    payload: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    if payload is None or "zones_filled" not in payload:
        return {"filled": False,
                "reason": f"fill process produced no JSON result: "
                          f"{result.stdout.strip()[-500:]!r}"}

    result_out: dict[str, Any] = {
        "filled": bool(payload.get("filled")),
        "zones_filled": int(payload["zones_filled"]),
        "interpreter": str(interpreter),
    }
    interp_major = _interpreter_major(interpreter)
    if (result_out["filled"] and format_major and interp_major
            and interp_major != format_major):
        result_out["warnings"] = [
            f"board was saved by KiCad {format_major} but filled with "
            f"KiCad {interp_major} (no KiCad {format_major} interpreter "
            f"found); the file is now in KiCad {interp_major} format"
        ]
    return result_out
