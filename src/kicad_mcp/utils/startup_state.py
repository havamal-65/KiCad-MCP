"""In-process record of when the MCP server last launched pcbnew (#20 / R1).

The bridge cannot positively distinguish "board still loading" from "no board
open" — ``pcbnew.GetBoard()`` returns None/empty during a load, and pcbnew
exposes no loading flag to the bridge. On a bridge-only path (IPC unavailable),
``get_startup_checklist`` would therefore report "No board loaded" even while a
board the server just told pcbnew to open is mid-load.

``open_kicad`` and ``get_startup_checklist`` run in the same long-lived MCP
server process, so a module-level timestamp set at launch lets the checklist
report "still loading — wait" during the load window instead, matching what
``open_kicad`` itself returns (``bridge:"pending"``) and what the IPC path
already distinguishes.

Limitation (documented in MCP-KNOWN-ISSUES #20): this only covers launches the
server initiated. A user who launches pcbnew themselves with IPC off still sees
"No board loaded" while the board loads — unaddressable without a pcbnew loading
API.
"""

from __future__ import annotations

import time

# Default load window: how long after a launch we assume a board may still be
# loading. pcbnew opens a board well within this on typical hardware; the window
# only affects the checklist's *wording* (still-loading vs no-board), never a
# gate outcome, so a generous value is safe.
DEFAULT_LOAD_WINDOW_S = 30.0

_last_launch_monotonic: float | None = None


def note_launch() -> None:
    """Record that pcbnew was just launched (starts the load window)."""
    global _last_launch_monotonic
    _last_launch_monotonic = time.monotonic()


def recent_launch(window_s: float = DEFAULT_LOAD_WINDOW_S) -> bool:
    """Return True if pcbnew was launched within *window_s* seconds.

    False if no launch has been recorded this process.
    """
    if _last_launch_monotonic is None:
        return False
    return (time.monotonic() - _last_launch_monotonic) < window_s


def reset_launch_state() -> None:
    """Clear the recorded launch time (test hook)."""
    global _last_launch_monotonic
    _last_launch_monotonic = None
