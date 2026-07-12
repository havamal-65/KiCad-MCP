"""REQ-FIX-3 (F2/S3, #16) — scratch-fixture integrity during a live run.

The post-session byte-exact guarantee (sha256 before == after) is enforced by
construction: ``scratch_board_guard`` in conftest restores the session-start
snapshot at teardown. Byte equality is NOT assertable mid-run — live tests
legitimately mutate copper/positions while they execute — so this test asserts
the invariant that IS checkable at any point of the run:

* the footprint-reference multiset is unchanged from session start
  (nothing leaked so far), and
* no reference appears twice (#16 duplicate-ref corruption tripwire — this
  is exactly the 13→26 poisoning that invalidated two live batches).

Per-test attribution of a leak happens in the autouse ``scratch_ref_hygiene``
fixture; this test is the batch-level checkpoint.
"""

from __future__ import annotations

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call

from .conftest import _SCRATCH_BOARD, _scratch_ref_counts


@pytest.mark.integration
def test_scratch_refs_intact_and_duplicate_free(bridge_session, scratch_board_guard):
    if scratch_board_guard is None:
        pytest.skip("scratch board not present")

    # Flush live state so the disk file is truthful. If the open board is a
    # different one, the scratch file on disk is already authoritative.
    try:
        _tcp_call("save_board", 10.0, path=str(_SCRATCH_BOARD))
    except Exception:
        pass

    current = _scratch_ref_counts()

    duplicates = {ref: n for ref, n in current.items() if n > 1}
    assert duplicates == {}, (
        f"scratch board carries DUPLICATE refs (#16 corruption): {duplicates}"
    )
    assert current == scratch_board_guard["ref_counts"], (
        "scratch board ref multiset diverged from session start — "
        f"start={scratch_board_guard['ref_counts']}, now={current}"
    )
