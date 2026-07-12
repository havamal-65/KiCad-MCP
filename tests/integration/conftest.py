"""Pytest fixtures for KiCad MCP integration tests.

These tests run against a real KiCad pcbnew process with the kicad_mcp_bridge
plugin loaded. They are skipped by default; opt in by setting
``KICAD_INTEGRATION=1`` and having pcbnew open with a board.

Run locally:
    1. Open pcbnew with any .kicad_pcb file (e.g. ``examples/`` or a fresh board)
    2. Verify the bridge log shows "TCP server started"
    3. ``$env:KICAD_INTEGRATION="1"; pytest tests/integration -v``

See ``tests/integration/README.md`` for full setup.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
from pathlib import Path

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call

_SCRATCH_BOARD = Path(__file__).parent.parent / "_scratch" / "test_board.kicad_pcb"


def _integration_enabled() -> bool:
    return os.environ.get("KICAD_INTEGRATION", "").lower() in ("1", "true", "yes")


def _bridge_port() -> int:
    return int(os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760"))


def _bridge_reachable(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def bridge_session():
    """Session fixture: gates every integration test on a live bridge.

    Skips with a clear, actionable message when:
      - ``KICAD_INTEGRATION`` is unset (default for normal pytest runs)
      - The bridge TCP port is not reachable
      - The bridge responds but ``ping`` does not return ``{"pong": True}``

    Yields the result of the ``ping`` handler (kicad_version included).
    """
    if not _integration_enabled():
        pytest.skip(
            "Integration tests disabled. Set KICAD_INTEGRATION=1 and ensure "
            "pcbnew is running with the kicad_mcp_bridge plugin loaded."
        )

    port = _bridge_port()
    if not _bridge_reachable(port):
        pytest.skip(
            f"Bridge not reachable on localhost:{port}. "
            "Open pcbnew and verify the bridge log shows 'TCP server started'."
        )

    result = _tcp_call("ping", timeout=2.0)
    if not isinstance(result, dict) or not result.get("pong"):
        pytest.fail(
            f"Bridge reachable but ping returned unexpected result: {result!r}"
        )

    yield result


# ---------------------------------------------------------------------------
# Scratch-fixture hygiene (F2/S3, #16 — REQ-FIX-1/REQ-FIX-3)
# ---------------------------------------------------------------------------

def _scratch_ref_counts() -> dict[str, int]:
    """Footprint-reference multiset of the scratch board on disk."""
    if not _SCRATCH_BOARD.exists():
        return {}
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
    content = _SCRATCH_BOARD.read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    for m in re.finditer(r'\(footprint\s+"', content):
        end = _walk_balanced_parens(content, m.start())
        if end is None:
            continue
        block = content[m.start():end + 1]
        ref_m = re.search(r'\(property "Reference" "([^"]+)"', block)
        if ref_m:
            counts[ref_m.group(1)] = counts.get(ref_m.group(1), 0) + 1
    return counts


@pytest.fixture(scope="session", autouse=True)
def scratch_board_guard():
    """REQ-FIX-3: byte-snapshot the scratch fixture at session start, restore
    it at session end — a full integration run leaves the file byte-exact by
    construction, so no run can accumulate corruption for the next one.

    NOTE for interactive sessions: after the restore, a still-open pcbnew
    holds a diverged in-memory board. Reopen the board (or restart pcbnew)
    before the next live run — the #14C stale-board guard refuses mutations
    until then. The batch protocol already relaunches pcbnew per run.
    """
    if not _integration_enabled() or not _SCRATCH_BOARD.exists():
        yield None
        return
    snapshot = _SCRATCH_BOARD.read_bytes()
    yield {
        "sha256": hashlib.sha256(snapshot).hexdigest(),
        "ref_counts": _scratch_ref_counts(),
    }
    if _SCRATCH_BOARD.read_bytes() != snapshot:
        _SCRATCH_BOARD.write_bytes(snapshot)


@pytest.fixture(autouse=True)
def scratch_ref_hygiene():
    """REQ-FIX-1, loud and attributed: a test that adds a footprint ref to
    the scratch board without removing it — or worse, creates a duplicate
    ref (#16) — fails at ITS OWN teardown, not as mystery fallout three
    tests later."""
    if not _integration_enabled() or not _SCRATCH_BOARD.exists():
        yield
        return
    before = _scratch_ref_counts()
    yield
    after = _scratch_ref_counts()
    leaked = {ref: n for ref, n in after.items() if n > before.get(ref, 0)}
    if leaked:
        dupes = {ref: n for ref, n in leaked.items() if after[ref] > 1}
        detail = f" — DUPLICATE REFS (#16 corruption): {dupes}" if dupes else ""
        raise AssertionError(
            f"test leaked footprint refs on the scratch board: {leaked}"
            f"{detail}. REQ-FIX-1: remove what you place (try/finally)."
        )
