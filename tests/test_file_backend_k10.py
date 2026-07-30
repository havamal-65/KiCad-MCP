"""K10-S2 FMT-001/FMT-002 — KiCad 10 file-format round-trips, no KiCad required.

The fixture ``tests/fixtures/boards/k10/k10_saved.kicad_pcb`` is the live
scratch board re-saved by KiCad 10.0.4 (format 20260206): no top-level net
table, name-only ``(net "NAME")`` references everywhere, and the new
padstack/footprint tokens (``tenting``/``covering``/``plugging``/``capping``/
``duplicate_pad_numbers_are_jumpers``). Every file-backend mutation on such a
board must (a) follow the file's own name-only convention — a coded clause in
a 10-format file is silently ignored on load — and (b) pass the v10 tokens
through untouched.

``k10_saved.kicad_sch`` (format 20260306, ``kicad-cli 10 sch upgrade``) covers
the schematic side: the 10 pretty-printer puts a newline after ``(symbol``
(≤9 wrote ``(symbol (lib_id`` on one line), which broke every writer that
located instance blocks with a literal ``"(symbol "`` scan.
"""

from __future__ import annotations

import re
import shutil
import textwrap
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import (
    FileBoardOps,
    FileSchematicOps,
    _file_uses_net_codes,
)
from kicad_mcp.utils.sexp_parser import (
    find_footprint_block_by_reference,
    find_symbol_block_by_reference,
)

_K10_FIXTURE = (
    Path(__file__).parent / "fixtures" / "boards" / "k10" / "k10_saved.kicad_pcb"
)
_K10_SCH_FIXTURE = (
    Path(__file__).parent / "fixtures" / "boards" / "k10" / "k10_saved.kicad_sch"
)

_V10_TOKENS = (
    "(tenting",
    "(covering",
    "(plugging",
    "(capping",
    "(duplicate_pad_numbers_are_jumpers",
)

_CODED_NET_RE = re.compile(r"\(net\s+\d")


@pytest.fixture
def k10_board(tmp_path: Path) -> Path:
    board = tmp_path / "k10_saved.kicad_pcb"
    shutil.copy(_K10_FIXTURE, board)
    return board


@pytest.fixture
def k10_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "k10_saved.kicad_sch"
    shutil.copy(_K10_SCH_FIXTURE, sch)
    return sch


def _token_counts(content: str) -> dict[str, int]:
    return {tok: content.count(tok) for tok in _V10_TOKENS}


# ---------------------------------------------------------------------------
# Fixture integrity — everything below is meaningless if this drifts
# ---------------------------------------------------------------------------

def test_fixture_is_kicad10_format():
    content = _K10_FIXTURE.read_text(encoding="utf-8")
    assert "(version 20260206)" in content
    assert not _CODED_NET_RE.search(content), (
        "k10 fixture must be name-only — a coded (net N …) clause means it "
        "was regenerated with a ≤9 writer"
    )
    for tok in _V10_TOKENS:
        assert tok in content, f"k10 fixture lost the v10 token {tok!r}"
    # Name-only references present in both pad and segment positions.
    assert '(net "T10_GND")' in content


def test_file_uses_net_codes_false_for_k10():
    content = _K10_FIXTURE.read_text(encoding="utf-8")
    assert _file_uses_net_codes(content) is False


def test_file_uses_net_codes_true_for_coded_preamble():
    coded = textwrap.dedent('''\
        (kicad_pcb (version 20241229) (generator "pcbnew")
          (net 0 "")
          (net 1 "GND")
          (footprint "Resistor_SMD:R_0805_2012Metric")
        )
    ''')
    assert _file_uses_net_codes(coded) is True


# ---------------------------------------------------------------------------
# Read path — parsers on a 10-saved board
# ---------------------------------------------------------------------------

def test_get_components_parses_k10(k10_board: Path):
    ops = FileBoardOps()
    refs = {c["reference"] for c in ops.get_components(k10_board)}
    assert refs == {"T10_U1", "T14_R1", "T14_R2", "T14_R3", "T14_R4"}


def test_get_nets_name_only_fallback(k10_board: Path):
    ops = FileBoardOps()
    nets = ops.get_nets(k10_board)
    assert nets, "no top-level table — nets must come from name-only refs"
    assert all(n["number"] is None for n in nets)
    assert "T10_GND" in {n["name"] for n in nets}


def test_get_tracks_parses_name_only_segment(k10_board: Path):
    ops = FileBoardOps()
    tracks = ops.get_tracks(k10_board)
    assert tracks, "fixture carries one routed segment"
    assert all("start" in t and "end" in t for t in tracks)


# ---------------------------------------------------------------------------
# Write path — mutations follow the file's own convention
# ---------------------------------------------------------------------------

def test_move_component_preserves_v10_tokens(k10_board: Path):
    before = _token_counts(k10_board.read_text(encoding="utf-8"))
    ops = FileBoardOps()
    result = ops.move_component(k10_board, "T14_R1", x=12.5, y=34.5)
    assert result["position"]["x"] == pytest.approx(12.5)

    after_content = k10_board.read_text(encoding="utf-8")
    assert _token_counts(after_content) == before
    assert not _CODED_NET_RE.search(after_content)


def test_add_track_emits_name_only_net(k10_board: Path):
    ops = FileBoardOps()
    ops.add_track(
        k10_board, start_x=10.0, start_y=10.0, end_x=20.0, end_y=10.0,
        width=0.25, net="T10_GND",
    )
    content = k10_board.read_text(encoding="utf-8")
    assert re.search(
        r'\(segment \(start 10\.0 10\.0\).*?\(net "T10_GND"\)', content
    ), "new segment must carry the name-only clause"
    assert not _CODED_NET_RE.search(content), (
        "a coded (net N) clause in a 10-format file is ignored by KiCad 10"
    )


def test_add_via_emits_name_only_net(k10_board: Path):
    ops = FileBoardOps()
    ops.add_via(k10_board, x=15.0, y=15.0, net="T10_GND")
    content = k10_board.read_text(encoding="utf-8")
    assert re.search(
        r'\(via \(at 15\.0 15\.0\).*?\(net "T10_GND"\)', content
    )
    assert not _CODED_NET_RE.search(content)


def test_assign_net_existing_net_name_only(k10_board: Path):
    ops = FileBoardOps()
    result = ops.assign_net(k10_board, "T14_R1", pad="1", net="T10_GND")
    assert result["pads_updated"] >= 1

    content = k10_board.read_text(encoding="utf-8")
    located = find_footprint_block_by_reference(content, "T14_R1")
    assert located is not None
    block = content[located[0]:located[1] + 1]
    assert '(net "T10_GND")' in block
    assert not _CODED_NET_RE.search(content)


def test_assign_net_new_net_adds_no_net_table(k10_board: Path):
    """A new net on a name-only board is born by reference alone — the
    writer must not invent a numbered top-level table entry."""
    ops = FileBoardOps()
    ops.assign_net(k10_board, "T14_R2", pad="2", net="K10_NEW_NET")

    content = k10_board.read_text(encoding="utf-8")
    assert '(net "K10_NEW_NET")' in content
    assert not re.search(r'(?m)^[ \t]*\(net\s+\d+\s+"', content), (
        "10-format file grew a ≤9-style numbered net table"
    )

    nets = {n["name"] for n in ops.get_nets(k10_board)}
    assert "K10_NEW_NET" in nets


def test_assign_net_replaces_existing_name_only_clause(k10_board: Path):
    """Re-assigning a pad that already carries a name-only clause must
    replace it, not append a duplicate (net …) node."""
    ops = FileBoardOps()
    ops.assign_net(k10_board, "T14_R3", pad="1", net="FIRST_NET")
    ops.assign_net(k10_board, "T14_R3", pad="1", net="SECOND_NET")

    content = k10_board.read_text(encoding="utf-8")
    located = find_footprint_block_by_reference(content, "T14_R3")
    assert located is not None
    block = content[located[0]:located[1] + 1]
    assert '(net "SECOND_NET")' in block
    assert '(net "FIRST_NET")' not in block


def test_remove_component_takes_only_its_tokens(k10_board: Path):
    content = k10_board.read_text(encoding="utf-8")
    before = _token_counts(content)
    located = find_footprint_block_by_reference(content, "T14_R4")
    assert located is not None
    removed_share = _token_counts(content[located[0]:located[1] + 1])

    ops = FileBoardOps()
    result = ops.remove_component(k10_board, "T14_R4")
    assert result["removed"] is True

    after = _token_counts(k10_board.read_text(encoding="utf-8"))
    assert after == {
        tok: before[tok] - removed_share[tok] for tok in _V10_TOKENS
    }


def test_mutation_chain_round_trip(k10_board: Path):
    """FMT-001 V-A: a realistic parse→mutate→save chain leaves the file
    parseable, name-only, and token-complete."""
    ops = FileBoardOps()
    before = _token_counts(k10_board.read_text(encoding="utf-8"))

    ops.move_component(k10_board, "T14_R2", x=50.0, y=40.0, rotation=90.0)
    ops.assign_net(k10_board, "T14_R2", pad="1", net="RT_NET")
    ops.add_track(
        k10_board, start_x=50.0, start_y=40.0, end_x=55.0, end_y=40.0,
        width=0.25, net="RT_NET",
    )
    ops.add_via(k10_board, x=55.0, y=40.0, net="RT_NET")

    content = k10_board.read_text(encoding="utf-8")
    assert _token_counts(content) == before
    assert not _CODED_NET_RE.search(content)
    assert "(version 20260206)" in content

    # The whole file still parses: every reader returns sane data.
    refs = {c["reference"] for c in ops.get_components(k10_board)}
    assert "T14_R2" in refs
    assert "RT_NET" in {n["name"] for n in ops.get_nets(k10_board)}
    assert len(ops.get_tracks(k10_board)) >= 2


# ---------------------------------------------------------------------------
# Schematic — format 20260306 (newline after "(symbol")
# ---------------------------------------------------------------------------

def test_sch_fixture_is_kicad10_format():
    content = _K10_SCH_FIXTURE.read_text(encoding="utf-8")
    assert "(version 20260306)" in content
    assert 'generator_version "10.0"' in content
    assert re.search(r"\(symbol\r?\n", content), (
        "the 10 pretty-printer newline after (symbol is the very thing "
        "under test — a fixture without it was regenerated with a ≤9 writer"
    )


def test_sch_get_symbols_parses_k10(k10_sch: Path):
    ops = FileSchematicOps()
    refs = {s["reference"] for s in ops.get_symbols(k10_sch)}
    assert len(refs) >= 30
    assert "C1" in refs


def test_sch_find_symbol_block_newline_form(k10_sch: Path):
    """Regression: the locator scanned for a literal ``"(symbol "`` and
    missed every instance in a 10-saved schematic — reads worked (sexp
    tree), all writes raised 'not found'."""
    content = k10_sch.read_text(encoding="utf-8")
    located = find_symbol_block_by_reference(content, "C1")
    assert located is not None
    block = content[located[0]:located[1] + 1]
    assert '(property "Reference" "C1"' in block
    assert '(lib_id "Device:C")' in block


def test_sch_update_property_round_trip(k10_sch: Path):
    ops = FileSchematicOps()
    ops.update_component_property(k10_sch, "C1", "K10_PROBE", "on")
    content = k10_sch.read_text(encoding="utf-8")
    located = find_symbol_block_by_reference(content, "C1")
    assert located is not None
    assert '(property "K10_PROBE" "on"' in content[located[0]:located[1] + 1]
    # Still parseable, symbol count unchanged.
    assert len(ops.get_symbols(k10_sch)) >= 30


def test_sch_move_component_round_trip(k10_sch: Path):
    ops = FileSchematicOps()
    result = ops.move_component(k10_sch, "C1", x=123.0, y=45.5)
    assert result["position"]["x"] == pytest.approx(123.0)
    moved = next(
        s for s in FileSchematicOps().get_symbols(k10_sch)
        if s["reference"] == "C1"
    )
    assert moved["position"]["x"] == pytest.approx(123.0)
    assert moved["position"]["y"] == pytest.approx(45.5)
