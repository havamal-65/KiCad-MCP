"""REQ-METRIC-006 / REQ-TEST-P1-002 — live ground-truth (V-M, opt-in).

The enforceable R5 gate (metric ranks a worse placement worse than a better one
of the same netlist) is proven deterministically and on every CI run in
``tests/test_placement_groundtruth.py``. This opt-in companion adds the V-M
"real geometry" leg (REQ-TRUTH-001): it scores boards authored by *real KiCad*
to confirm the pad/courtyard parser handles production footprint geometry — not
just hand-authored fixtures — and that scoring is byte-deterministic on real
files.

Run with a live bridge:
    $env:KICAD_INTEGRATION="1"; pytest tests/integration/test_placement_metric_groundtruth.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.placement_metrics import placement_metric

# Real KiCad-authored fixtures already committed under tests/fixtures/boards/.
_REAL_BOARDS = [
    "ses_value_revert_v1.kicad_pcb",
    "bt_audio_v1_before_connector_fix.kicad_pcb",
]


@pytest.mark.integration
@pytest.mark.parametrize("board_name", _REAL_BOARDS)
def test_metric_on_real_board_geometry(board_name: str) -> None:
    """placement_metric produces a well-formed, deterministic bundle on real boards.

    No bridge needed for parsing real bytes, but this is kept opt-in so it runs
    alongside the live V-M leg. Asserts the bundle shape (REQ-METRIC-005) and
    determinism (REQ-DET-001) against geometry KiCad actually wrote.
    """
    board = Path(__file__).parent.parent / "fixtures" / "boards" / board_name
    if not board.exists():
        pytest.skip(f"fixture {board_name} not present")

    bundle = placement_metric(board)

    # Stable bundle shape (REQ-METRIC-005).
    for key in (
        "total_hpwl_mm", "overlap_count", "out_of_outline_count",
        "decap_max_mm", "decap_mean_mm", "orientation_consistency",
        "signal_net_count", "scored_parts",
    ):
        assert key in bundle, f"missing bundle key {key!r}"

    assert isinstance(bundle["total_hpwl_mm"], float)
    assert bundle["total_hpwl_mm"] >= 0.0
    # P2 populates decap proximity: either None (no decoupling caps on the board)
    # or a non-negative float (max/mean cap→IC centre distance, mm).
    for key in ("decap_max_mm", "decap_mean_mm"):
        val = bundle[key]
        assert val is None or (isinstance(val, float) and val >= 0.0), (key, val)
    assert 0.0 <= bundle["orientation_consistency"] <= 1.0
    assert bundle["scored_parts"] >= 0

    # Determinism on real file bytes.
    assert placement_metric(board) == bundle


@pytest.mark.integration
def test_live_board_ranks_tight_better(bridge_session: object) -> None:
    """V-M: score the currently-open live board; metric must run on real geometry.

    The full reconstruct-bad-vs-good comparison (spec §7.2) is enforced
    deterministically in the sibling unit test; here we confirm the metric runs
    end-to-end against whatever board the engineer has open in pcbnew and yields
    a sane, finite Total HPWL — the real-geometry trust check.
    """
    from kicad_mcp.backends.plugin_backend import _tcp_call

    info = _tcp_call("get_board_info", timeout=5.0)
    board_path = None
    if isinstance(info, dict):
        board_path = info.get("file_path") or info.get("path")
    if not board_path or not Path(board_path).exists():
        pytest.skip("no live board path available from the bridge")

    bundle = placement_metric(Path(board_path))
    assert isinstance(bundle["total_hpwl_mm"], float)
    assert bundle["total_hpwl_mm"] >= 0.0
    assert bundle["signal_net_count"] >= 0
