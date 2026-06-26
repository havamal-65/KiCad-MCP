"""Tests for §6.8 sync-failure footprint suggestions (REQ-TEST-001…005).

The suggester ranks replacement footprints by name; the sync wiring enriches the
§6.2 ``symbol_footprint_validator_failed`` refusal with per-unresolvable
``candidates``. Helper tests mock ``library_ops``; wiring tests patch the
validator to force the failure shape (no schematic fixture / library install).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import fastmcp

from kicad_mcp.tools import schematic as schematic_mod
from kicad_mcp.tools.schematic import suggest_footprint_candidates
from kicad_mcp.utils.change_log import ChangeLog


# ── REQ-TEST-001 — ranking ───────────────────────────────────────────────────

def test_ranking_exact_prefix_substring_excludes_self_and_truncates():
    bad = "BadLib:SOIC-8"
    lib_ops = MagicMock()
    lib_ops.search_footprints.return_value = [
        {"name": "SOIC-8", "library": "BadLib", "lib_id": "BadLib:SOIC-8"},        # self → excluded
        {"name": "X_SOIC-8_extra", "library": "L3", "lib_id": "L3:X_SOIC-8_extra"},  # substring
        {"name": "SOIC-8", "library": "Package_SO", "lib_id": "Package_SO:SOIC-8"},   # exact (diff lib)
        {"name": "SOIC-8_HandSolder", "library": "L2", "lib_id": "L2:SOIC-8_HandSolder"},  # prefix
        {"name": "SOIC-8_alt", "library": "L4", "lib_id": "L4:SOIC-8_alt"},          # prefix
    ]

    out = suggest_footprint_candidates(bad, library_ops=lib_ops, limit=5)

    assert all(c["lib_id"] != bad for c in out)  # self excluded (REQ-RANK-003)
    assert out[0]["match_reason"] == "exact_name"
    assert out[0]["lib_id"] == "Package_SO:SOIC-8"
    reasons = [c["match_reason"] for c in out]
    # exact first, then the two prefixes, then the substring last.
    assert reasons == ["exact_name", "prefix", "prefix", "substring"]
    assert len(out) <= 5


def test_ranking_truncates_to_limit():
    lib_ops = MagicMock()
    lib_ops.search_footprints.return_value = [
        {"name": f"R_0805_v{i}", "library": "L", "lib_id": f"L:R_0805_v{i}"}
        for i in range(10)
    ]
    out = suggest_footprint_candidates("X:R_0805", library_ops=lib_ops, limit=3)
    assert len(out) == 3


# ── REQ-TEST-002 — never raises ──────────────────────────────────────────────

def test_helper_never_raises():
    lib_ops = MagicMock()
    lib_ops.search_footprints.side_effect = RuntimeError("library scan blew up")
    assert suggest_footprint_candidates("Lib:Part", library_ops=lib_ops) == []


def test_package_prefix_retry_broadens_search():
    """When the full-name search is short, the package-prefix token is retried."""
    lib_ops = MagicMock()

    def _search(q):
        if q == "QFP-48_7x7mm_P0.5mm":
            return []  # exact variant gone
        if q == "QFP-48":
            return [{"name": "QFP-48_7x7mm_P0.4mm", "library": "Package_QFP",
                     "lib_id": "Package_QFP:QFP-48_7x7mm_P0.4mm"}]
        return []

    lib_ops.search_footprints.side_effect = _search
    out = suggest_footprint_candidates("OldLib:QFP-48_7x7mm_P0.5mm", library_ops=lib_ops)
    assert len(out) == 1
    assert out[0]["lib_id"] == "Package_QFP:QFP-48_7x7mm_P0.4mm"


# ── sync wiring fixtures ─────────────────────────────────────────────────────

def _sync_fn(backend, tmp_path: Path):
    mcp = fastmcp.FastMCP("test")
    schematic_mod.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values()
        if t.name == "sync_schematic_to_pcb"
    )


def _files(tmp_path: Path) -> tuple[str, str]:
    sch = tmp_path / "b.kicad_sch"
    pcb = tmp_path / "b.kicad_pcb"
    sch.write_text("(kicad_sch (version 20240101))", encoding="utf-8")
    pcb.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    return str(sch), str(pcb)


_FAIL_SF = {
    "passed": False,
    "mismatches": [],
    "unresolvable": [{"ref": "U1", "footprint": "BadLib:NoSuchFP",
                      "reason": "library not found"}],
}


# ── REQ-TEST-003 — sync enrichment, no PCB op ────────────────────────────────

def test_sync_enriches_unresolvable_with_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr("kicad_mcp.tools.drc.run_validate_symbol_footprint_pairs",
                        lambda p: copy.deepcopy(_FAIL_SF))
    monkeypatch.setattr(
        "kicad_mcp.backends.file_backend.FileLibraryOps.search_footprints",
        lambda self, q: [{"name": "NoSuchFP", "library": "Other", "lib_id": "Other:NoSuchFP"}],
    )
    backend = MagicMock()
    sch, pcb = _files(tmp_path)

    result = json.loads(_sync_fn(backend, tmp_path)(sch, pcb))

    assert result["status"] == "blocked"
    assert result["reason"] == "symbol_footprint_validator_failed"
    cands = result["unresolvable"][0]["candidates"]
    assert cands and cands[0]["lib_id"] == "Other:NoSuchFP"
    assert "candidates" in result["message"]
    # No PCB read/modify was attempted — the precondition fires first.
    backend.get_board_ops.assert_not_called()
    backend.get_board_modify_ops.assert_not_called()


# ── REQ-TEST-004 — additivity + passing path ─────────────────────────────────

def test_enrichment_is_additive(tmp_path, monkeypatch):
    monkeypatch.setattr("kicad_mcp.tools.drc.run_validate_symbol_footprint_pairs",
                        lambda p: copy.deepcopy(_FAIL_SF))
    monkeypatch.setattr(
        "kicad_mcp.backends.file_backend.FileLibraryOps.search_footprints",
        lambda self, q: [{"name": "NoSuchFP", "library": "Other", "lib_id": "Other:NoSuchFP"}],
    )
    sch, pcb = _files(tmp_path)
    result = json.loads(_sync_fn(MagicMock(), tmp_path)(sch, pcb))

    assert result["status"] == "blocked"
    assert result["reason"] == "symbol_footprint_validator_failed"
    assert result["mismatches"] == []
    entry = result["unresolvable"][0]
    assert entry["ref"] == "U1"
    assert entry["footprint"] == "BadLib:NoSuchFP"
    assert entry["reason"] == "library not found"


def test_passing_validator_runs_no_candidate_machinery(tmp_path, monkeypatch):
    monkeypatch.setattr("kicad_mcp.tools.drc.run_validate_symbol_footprint_pairs",
                        lambda p: {"passed": True, "mismatches": [], "unresolvable": []})
    called = {"search": False}

    def _spy(self, q):
        called["search"] = True
        return []

    monkeypatch.setattr(
        "kicad_mcp.backends.file_backend.FileLibraryOps.search_footprints", _spy,
    )
    sch, pcb = _files(tmp_path)
    backend = MagicMock()
    # Make the backend reads raise so the tool returns past the precondition but
    # we don't need a real board — we only care the precondition didn't block.
    backend.get_schematic_ops.return_value.read_schematic.side_effect = RuntimeError("stop here")

    out = _sync_fn(backend, tmp_path)(sch, pcb)
    result = json.loads(out)

    # Did NOT block on symbol_footprint_validator_failed → proceeded past §6.2.
    assert result.get("reason") != "symbol_footprint_validator_failed"
    assert called["search"] is False


# ── REQ-TEST-005 — empty candidates ──────────────────────────────────────────

def test_empty_candidates_no_hint(tmp_path, monkeypatch):
    monkeypatch.setattr("kicad_mcp.tools.drc.run_validate_symbol_footprint_pairs",
                        lambda p: copy.deepcopy(_FAIL_SF))
    monkeypatch.setattr(
        "kicad_mcp.backends.file_backend.FileLibraryOps.search_footprints",
        lambda self, q: [],
    )
    sch, pcb = _files(tmp_path)
    result = json.loads(_sync_fn(MagicMock(), tmp_path)(sch, pcb))

    assert result["unresolvable"][0]["candidates"] == []
    assert "candidates" not in result["message"]
    assert result["status"] == "blocked"
