"""Standalone helper for pcbnew operations via KiCad's bundled Python interpreter.

This script is invoked as a subprocess when the ``pcbnew`` module is not
importable in the MCP server's own Python environment (common on macOS and
Windows where KiCad ships its own Python).

Instead of generating code strings at call-time, operations are expressed as
named subcommands with arguments passed on the command line. Results are
returned as a single line of JSON on stdout so the caller can parse them
without fragile text matching.

Usage::

    python pcbnew_helper.py export_dsn <board_path> <dsn_path>
    python pcbnew_helper.py import_ses <board_path> <ses_path>
    python pcbnew_helper.py clean_board <board_path> <remove_keepouts> <remove_unassigned>
"""

from __future__ import annotations

import json
import sys


def _ok(**kwargs: object) -> None:
    print(json.dumps({"ok": True, **kwargs}))


def _fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(1)


def export_dsn(board_path: str, dsn_path: str) -> None:
    """Export board to Specctra DSN format."""
    import pcbnew  # type: ignore[import-untyped]

    board = pcbnew.LoadBoard(board_path)
    ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
    if not ok:
        _fail("ExportSpecctraDSN returned False — check for duplicate reference designators")
        return
    _ok()


def import_ses(board_path: str, ses_path: str) -> None:
    """Import a routed Specctra SES session into the board."""
    import pcbnew  # type: ignore[import-untyped]

    board = pcbnew.LoadBoard(board_path)
    tracks_before = len(board.GetTracks())
    ok = pcbnew.ImportSpecctraSES(board, ses_path)
    if not ok:
        _fail("ImportSpecctraSES returned False")
        return
    tracks_after = len(board.GetTracks())
    pcbnew.SaveBoard(board_path, board)
    _ok(tracks_before=tracks_before, tracks_after=tracks_after)


def clean_board(board_path: str, remove_keepouts: str, remove_unassigned: str) -> None:
    """Remove keepout zones and/or unassigned tracks from the board."""
    import pcbnew  # type: ignore[import-untyped]

    do_keepouts = remove_keepouts.lower() == "true"
    do_tracks = remove_unassigned.lower() == "true"

    board = pcbnew.LoadBoard(board_path)
    keepouts_removed = 0
    tracks_removed = 0

    if do_keepouts:
        zones = [z for z in board.Zones() if z.GetIsRuleArea()]
        for z in zones:
            board.Remove(z)
        keepouts_removed = len(zones)

    if do_tracks:
        bad = [
            t for t in board.GetTracks()
            if not (t.GetNet() and t.GetNet().GetNetname())
        ]
        for t in bad:
            board.Remove(t)
        tracks_removed = len(bad)

    pcbnew.SaveBoard(board_path, board)
    _ok(keepouts_removed=keepouts_removed, tracks_removed=tracks_removed)


_COMMANDS = {
    "export_dsn": (export_dsn, 2),
    "import_ses": (import_ses, 2),
    "clean_board": (clean_board, 3),
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _fail("No command provided")

    cmd = args[0]
    if cmd not in _COMMANDS:
        _fail(f"Unknown command: {cmd!r}. Valid commands: {list(_COMMANDS)}")

    func, nargs = _COMMANDS[cmd]
    if len(args) - 1 != nargs:
        _fail(f"{cmd} requires exactly {nargs} arguments, got {len(args) - 1}")

    try:
        func(*args[1:])
    except Exception as exc:
        _fail(str(exc))
