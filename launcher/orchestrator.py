"""Bring-up decision logic — pure, side-effect-free, import-safe (no GUI).

`plan_bringup` decides which of the three stack pieces to start given a status
snapshot (idempotent: running pieces are skipped). `classify_failures` turns a
signals snapshot into a list of user-facing diagnostics. Both operate on plain
data so they unit-test without mocking; the real probe-gathering that fills the
status/signals dicts lives in `launcher.processes.collect_signals` (M3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- status/signals keys (documented contract) ---
# status:  {"pcbnew_running": bool, "mcp_state": "ours"|"foreign"|"down"}
# signals: {"pcbnew_exe": Path|None, "claude_available": bool,
#           "mcp_state": "ours"|"foreign"|"down", "board": Path|None}

PIECES = ("kicad", "mcp", "claude")


@dataclass(frozen=True)
class Step:
    piece: str  # one of PIECES
    action: str  # "start" | "skip"
    reason: str


@dataclass(frozen=True)
class Diag:
    code: str
    message: str
    blocking: bool


def plan_bringup(status: dict[str, Any], board: Path | None) -> list[Step]:
    """Decide which pieces to start. Running pieces → skip; only down pieces
    get 'start'. A fresh Claude session is intended on every bring-up."""
    steps: list[Step] = []

    # KiCad (pcbnew)
    if status.get("pcbnew_running"):
        steps.append(Step("kicad", "skip", "pcbnew already running"))
    elif board is None:
        steps.append(Step("kicad", "skip", "no project selected"))
    else:
        steps.append(Step("kicad", "start", f"open pcbnew on {Path(board).name}"))

    # MCP server
    mcp_state = status.get("mcp_state", "down")
    if mcp_state == "ours":
        steps.append(Step("mcp", "skip", "MCP server already running"))
    elif mcp_state == "foreign":
        steps.append(
            Step("mcp", "skip", "port in use by a foreign process — not starting a second")
        )
    else:
        steps.append(Step("mcp", "start", "start MCP HTTP server"))

    # Claude Code — always a fresh session
    steps.append(Step("claude", "start", "launch Claude Code session"))

    return steps


def classify_failures(signals: dict[str, Any]) -> list[Diag]:
    """Turn a signals snapshot into blocking/non-blocking diagnostics."""
    diags: list[Diag] = []

    if signals.get("pcbnew_exe") is None:
        diags.append(
            Diag(
                "kicad_missing",
                "KiCad (pcbnew) executable not found — install KiCad or set its path.",
                blocking=True,
            )
        )

    if not signals.get("claude_available", False):
        diags.append(
            Diag(
                "claude_missing",
                "'claude' CLI not found on PATH — install Claude Code to launch a session.",
                blocking=False,
            )
        )

    if signals.get("mcp_state") == "foreign":
        diags.append(
            Diag(
                "port_in_use_foreign",
                "MCP port already in use by a non-launcher process — will not start a second server.",
                blocking=False,
            )
        )

    if signals.get("board") is None:
        diags.append(
            Diag(
                "no_project",
                "No project selected — pick a board first.",
                blocking=True,
            )
        )

    return diags


def has_blocking(diags: list[Diag]) -> bool:
    return any(d.blocking for d in diags)
