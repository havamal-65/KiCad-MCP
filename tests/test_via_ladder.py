"""REQ-FR-6 (F3/S4, #22) — adaptive via-cost ladder selection.

The via-cost completeness optimum is board-dependent and non-monotonic, so the
whole ladder is routed and the best result chosen: the fully-routed rung with
the fewest vias (tie-break shortest track length, then lowest via cost). If no
rung fully routes, the most-complete one is returned and flagged incomplete so
the caller reports ``partial`` — never a false ``success``.
"""

from __future__ import annotations

from kicad_mcp.tools.routing import _select_via_ladder_winner


def test_selects_fewest_via_complete():
    # Empirical anchor (aqs_v2, v2.2.4, pcbnew-verified): vc=200 → 3 vias, 0
    # unrouted is the fewest-via complete solution; vc=300 leaves 6 unrouted.
    rungs = [
        {"via_costs": 50, "unrouted": 0, "vias": 19, "track_length": 900.0},
        {"via_costs": 100, "unrouted": 0, "vias": 11, "track_length": 880.0},
        {"via_costs": 150, "unrouted": 1, "vias": 5, "track_length": 870.0},
        {"via_costs": 200, "unrouted": 0, "vias": 3, "track_length": 877.0},
        {"via_costs": 300, "unrouted": 6, "vias": 0, "track_length": 850.0},
    ]
    winner, complete = _select_via_ladder_winner(rungs)
    assert complete is True
    assert winner["via_costs"] == 200
    assert winner["vias"] == 3


def test_no_complete_returns_partial():
    rungs = [
        {"via_costs": 50, "unrouted": 4, "vias": 20, "track_length": 900.0},
        {"via_costs": 100, "unrouted": 2, "vias": 12, "track_length": 880.0},
        {"via_costs": 200, "unrouted": 7, "vias": 3, "track_length": 850.0},
    ]
    winner, complete = _select_via_ladder_winner(rungs)
    assert complete is False
    # Most-complete = fewest unrouted (2 at vc=100).
    assert winner["via_costs"] == 100
    assert winner["unrouted"] == 2


def test_tie_break_track_length():
    # Two complete solutions with equal vias → shorter track wins.
    rungs = [
        {"via_costs": 100, "unrouted": 0, "vias": 4, "track_length": 810.0},
        {"via_costs": 200, "unrouted": 0, "vias": 4, "track_length": 790.0},
    ]
    winner, complete = _select_via_ladder_winner(rungs)
    assert complete is True
    assert winner["via_costs"] == 200  # shorter track


def test_none_unrouted_not_treated_as_complete():
    # A rung whose unrouted is unknown (None) must not count as fully routed.
    rungs = [
        {"via_costs": 50, "unrouted": None, "vias": 8, "track_length": 900.0},
        {"via_costs": 100, "unrouted": 0, "vias": 10, "track_length": 880.0},
    ]
    winner, complete = _select_via_ladder_winner(rungs)
    assert complete is True
    assert winner["via_costs"] == 100  # the known-complete rung, not the None one
