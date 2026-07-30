"""K10-S4 units — variant authoring (REQ-S4B-001..009).

All V-A (no KiCad): pure writers exercised against the committed K10-S3
variant fixture's real grammar; the write session with an injected fake
oracle; the tool bodies with the capability gate monkeypatched both ways.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kicad_mcp.tools import variants as tool_mod
from kicad_mcp.utils import kicad_capabilities as kc
from kicad_mcp.utils import variant_files as vf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projects" / "variants"
FAKE_CLI = Path(r"C:\fake\kicad-cli.exe")


@pytest.fixture(autouse=True)
def _fresh_capability_cache() -> None:
    kc.clear_capability_cache()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Scratch copy of the committed variant fixture; returns the .kicad_pro."""
    for name in ("shift_reg_v1.kicad_pro", "shift_reg_v1.kicad_sch",
                 "shift_reg_v1.kicad_pcb"):
        shutil.copy2(FIXTURE_DIR / name, tmp_path / name)
    return tmp_path / "shift_reg_v1.kicad_pro"


def _texts(pro: Path) -> dict[str, str]:
    return {
        "pro": pro.read_text(encoding="utf-8"),
        "sch": pro.with_suffix(".kicad_sch").read_text(encoding="utf-8"),
        "pcb": pro.with_suffix(".kicad_pcb").read_text(encoding="utf-8"),
    }


class _FakeDRCOps:
    """Injected oracle: scripted ERC counts, always-parsing DRC."""

    erc_script: list[tuple[int, int]] = []
    erc_calls: int = 0
    write_report: bool = True

    def __init__(self, cli_path: Path) -> None:
        self.cli = cli_path

    def run_erc(self, sch: Path, output: Path | None = None) -> dict[str, Any]:
        idx = min(type(self).erc_calls, len(type(self).erc_script) - 1)
        type(self).erc_calls += 1
        errors, warnings = type(self).erc_script[idx]
        report = sch.parent / f"erc_{type(self).erc_calls}.json"
        if type(self).write_report:
            report.write_text("{}", encoding="utf-8")
        return {"report_file": str(report), "error_count": errors,
                "warning_count": warnings}

    def run_drc(self, pcb: Path, output: Path | None = None) -> dict[str, Any]:
        report = pcb.parent / "drc.json"
        if type(self).write_report:
            report.write_text("{}", encoding="utf-8")
        return {"report_file": str(report), "error_count": 0, "warning_count": 0}


@pytest.fixture
def fake_oracle() -> type[_FakeDRCOps]:
    _FakeDRCOps.erc_script = [(0, 80)]
    _FakeDRCOps.erc_calls = 0
    _FakeDRCOps.write_report = True
    with patch("kicad_mcp.backends.cli_backend.CLIDRCOps", _FakeDRCOps):
        yield _FakeDRCOps


@pytest.fixture
def gate_pass() -> None:
    """Capability gate resolves and passes (KiCad 10 present)."""
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli",
               return_value=FAKE_CLI), \
         patch.object(kc, "cli_supports", return_value=True), \
         patch("kicad_mcp.utils.platform_helper.is_pcbnew_running",
               return_value=False):
        yield


@pytest.fixture
def gate_refuse() -> None:
    """Capability gate resolves but refuses (KiCad 9 serving)."""
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli",
               return_value=FAKE_CLI), \
         patch.object(kc, "cli_supports", return_value=False), \
         patch.object(kc, "cli_version_echo", return_value="9.0.7"):
        yield


# ── format stamps ────────────────────────────────────────────────────────────

def test_file_format_version():
    assert vf.file_format_version("(kicad_sch\n\t(version 20260306)\n)") == 20260306
    assert vf.file_format_version("(kicad_pcb (version 20241229))") == 20241229
    assert vf.file_format_version("(kicad_sch)") is None


def test_check_format_versions_pass_on_fixture(project: Path):
    files = vf.resolve_project(project)
    assert vf.check_format_versions(files) is None


def test_check_format_versions_refuses_pre_v10(tmp_path: Path):
    (tmp_path / "old.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "old.kicad_sch").write_text(
        "(kicad_sch (version 20250114))", encoding="utf-8")
    files = vf.resolve_project(tmp_path / "old.kicad_pro")
    payload = vf.check_format_versions(files)
    assert payload is not None
    assert payload["error"] == "format_too_old"
    assert payload["found"] == 20250114
    assert payload["required"] == vf.SCH_MIN_FORMAT
    assert "upgrade" in payload["hint"].lower()


# ── .kicad_pro registry ──────────────────────────────────────────────────────

def test_pro_list_fixture(project: Path):
    entries = vf.pro_list_variants(_texts(project)["pro"])
    assert [e["name"] for e in entries] == ["Standard", "Lite"]
    assert "description" in entries[1] and "D8" in entries[1]["description"]


def test_pro_upsert_and_remove(project: Path):
    text = _texts(project)["pro"]
    added = vf.pro_upsert_variant(text, "Pro", "premium build")
    entries = vf.pro_list_variants(added)
    assert {"name": "Pro", "description": "premium build"} in entries
    # idempotent upsert → byte-identical
    assert vf.pro_upsert_variant(added, "Pro", "premium build") == added
    assert vf.pro_upsert_variant(added, "Pro") == added
    removed = vf.pro_remove_variant(added, "Pro")
    assert [e["name"] for e in vf.pro_list_variants(removed)] == ["Standard", "Lite"]
    # removing an absent name → byte-identical
    assert vf.pro_remove_variant(removed, "Nope") == removed


# ── .kicad_sch overrides ─────────────────────────────────────────────────────

def test_sch_list_fixture_overrides(project: Path):
    overrides = vf.sch_list_overrides(_texts(project)["sch"])
    assert overrides["R1"]["Lite"]["fields"] == {"Value": "4.7k"}
    assert overrides["D8"]["Lite"]["dnp"] is True
    assert "Standard" not in overrides.get("R1", {})


def test_sch_set_variant_insert_and_idempotent(project: Path):
    sch = _texts(project)["sch"]
    out = vf.sch_set_variant(sch, "R2", "Lite", vf.VariantOverrides(dnp=True))
    listing = vf.sch_list_overrides(out)
    assert listing["R2"]["Lite"] == {"dnp": True}
    # existing overrides untouched
    assert listing["R1"]["Lite"]["fields"] == {"Value": "4.7k"}
    # idempotent: same call converges to identical bytes
    assert vf.sch_set_variant(out, "R2", "Lite", vf.VariantOverrides(dnp=True)) == out


def test_sch_omit_when_equal_to_base(project: Path):
    """dnp=False equals the base attribute → name-only membership block."""
    sch = _texts(project)["sch"]
    out = vf.sch_set_variant(sch, "R2", "Lite", vf.VariantOverrides(dnp=False))
    assert vf.sch_list_overrides(out)["R2"]["Lite"] == {}
    loc = vf._find_symbol_path_block(out, "R2")
    assert loc is not None
    block = out[loc[2]:loc[3] + 1]
    assert "(variant" in block and "(dnp" not in block


def test_sch_field_equal_to_base_is_dropped(project: Path):
    """Setting R1's Value back to its base (220R) removes the field override."""
    sch = _texts(project)["sch"]
    base_fields = vf._base_fields(
        vf._sch_base_scope(sch, *vf._find_symbol_path_block(sch, "R1")[:2]))
    base_value = base_fields["Value"]
    out = vf.sch_set_variant(
        sch, "R1", "Lite", vf.VariantOverrides(fields={"Value": base_value}))
    assert vf.sch_list_overrides(out)["R1"]["Lite"] == {}


def test_sch_merge_keeps_existing_members(project: Path):
    """None params leave R1's existing field override intact while adding dnp."""
    sch = _texts(project)["sch"]
    out = vf.sch_set_variant(sch, "R1", "Lite", vf.VariantOverrides(dnp=True))
    members = vf.sch_list_overrides(out)["R1"]["Lite"]
    assert members == {"dnp": True, "fields": {"Value": "4.7k"}}


def test_sch_positive_logic_tokens(project: Path):
    sch = _texts(project)["sch"]
    out = vf.sch_set_variant(
        sch, "R3", "Lite",
        vf.VariantOverrides(in_bom=False, in_pos_files=False, exclude_from_sim=True))
    members = vf.sch_list_overrides(out)["R3"]["Lite"]
    assert members == {
        "in_bom": False, "in_pos_files": False, "exclude_from_sim": True,
    }
    loc = vf._find_symbol_path_block(out, "R3")
    block = out[loc[2]:loc[3] + 1]
    assert "(in_bom no)" in block and "(in_pos_files no)" in block
    assert "(exclude_from_sim yes)" in block


def test_sch_unknown_reference_raises(project: Path):
    with pytest.raises(vf.VariantAuthorError):
        vf.sch_set_variant(_texts(project)["sch"], "R99", "Lite",
                           vf.VariantOverrides(dnp=True))


def test_sch_remove_variant(project: Path):
    sch = _texts(project)["sch"]
    out, count = vf.sch_remove_variant(sch, "Lite")
    assert count == 2  # R1 + D8
    assert vf.sch_list_overrides(out) == {}
    again, count2 = vf.sch_remove_variant(out, "Lite")
    assert count2 == 0 and again == out


def test_sch_untouched_regions_preserved(project: Path):
    """Everything before the first edit point is byte-identical (REQ-S4B-007)."""
    sch = _texts(project)["sch"]
    out = vf.sch_set_variant(sch, "R2", "Lite", vf.VariantOverrides(dnp=True))
    loc = vf._find_symbol_path_block(sch, "R2")
    assert out[:loc[2]] == sch[:loc[2]]
    assert out.endswith(sch[loc[3] + 1:])


# ── .kicad_pcb board registry + footprint overrides ──────────────────────────

def test_pcb_list_fixture(project: Path):
    pcb = _texts(project)["pcb"]
    registry = vf.pcb_list_board_variants(pcb)
    assert [e["name"] for e in registry] == ["Standard", "Lite"]
    fp = vf.pcb_list_footprint_overrides(pcb)
    assert fp["D8"]["Lite"]["dnp"] is True
    assert fp["R1"]["Lite"]["fields"] == {"Value": "4.7k"}


def test_pcb_registry_upsert_and_remove(project: Path):
    pcb = _texts(project)["pcb"]
    added = vf.pcb_upsert_board_variant(pcb, "Pro", "premium")
    assert {"name": "Pro", "description": "premium"} in vf.pcb_list_board_variants(added)
    assert vf.pcb_upsert_board_variant(added, "Pro", "premium") == added
    removed = vf.pcb_remove_board_variant(added, "Pro")
    assert [e["name"] for e in vf.pcb_list_board_variants(removed)] == [
        "Standard", "Lite"]
    assert vf.pcb_remove_board_variant(removed, "Nope") == removed


def test_pcb_registry_created_when_absent(project: Path):
    pcb = _texts(project)["pcb"]
    stripped = vf.pcb_remove_board_variant(pcb, "Standard")
    stripped = vf.pcb_remove_board_variant(stripped, "Lite")
    assert vf.pcb_list_board_variants(stripped) == []
    assert "(variants" not in stripped  # empty registry removed entirely
    recreated = vf.pcb_upsert_board_variant(stripped, "Alpha", None)
    assert [e["name"] for e in vf.pcb_list_board_variants(recreated)] == ["Alpha"]
    # created in the surveyed position: after (setup …), before footprints
    assert recreated.index("(variants") < recreated.index("(footprint ")
    assert recreated.index("(setup") < recreated.index("(variants")


def test_pcb_negative_logic_inversion(project: Path):
    """in_bom=False / in_pos_files=False → the board file's exclude tokens."""
    pcb = _texts(project)["pcb"]
    out = vf.pcb_set_footprint_variant(
        pcb, "R3", "Lite", vf.VariantOverrides(in_bom=False, in_pos_files=False))
    members = vf.pcb_list_footprint_overrides(out)["R3"]["Lite"]
    assert members["exclude_from_bom"] is True
    assert members["exclude_from_pos_files"] is True
    span = vf.find_footprint_block_by_reference(out, "R3")
    block = out[span[0]:span[1] + 1]
    assert "(exclude_from_bom yes)" in block
    assert "(exclude_from_pos_files yes)" in block
    assert "(in_bom" not in block  # never the sch-side token on the board


def test_pcb_exclude_from_sim_ignored(project: Path):
    pcb = _texts(project)["pcb"]
    out = vf.pcb_set_footprint_variant(
        pcb, "R3", "Lite", vf.VariantOverrides(exclude_from_sim=True))
    members = vf.pcb_list_footprint_overrides(out).get("R3", {}).get("Lite", {})
    assert "exclude_from_sim" not in members


def test_pcb_set_idempotent_and_merge(project: Path):
    pcb = _texts(project)["pcb"]
    out = vf.pcb_set_footprint_variant(
        pcb, "R1", "Lite", vf.VariantOverrides(dnp=True))
    members = vf.pcb_list_footprint_overrides(out)["R1"]["Lite"]
    assert members == {"dnp": True, "fields": {"Value": "4.7k"}}
    assert vf.pcb_set_footprint_variant(
        out, "R1", "Lite", vf.VariantOverrides(dnp=True)) == out


def test_pcb_variant_block_position(project: Path):
    """New blocks land where pcbnew writes them: before embedded_fonts/model."""
    pcb = _texts(project)["pcb"]
    out = vf.pcb_set_footprint_variant(
        pcb, "R3", "Lite", vf.VariantOverrides(dnp=True))
    span = vf.find_footprint_block_by_reference(out, "R3")
    block = out[span[0]:span[1] + 1]
    variant_at = block.index("(variant")
    anchor = block.find("(embedded_fonts")
    if anchor == -1:
        anchor = block.find("(model ")
    assert anchor != -1 and variant_at < anchor


def test_pcb_unknown_reference_raises(project: Path):
    with pytest.raises(vf.VariantAuthorError):
        vf.pcb_set_footprint_variant(_texts(project)["pcb"], "R99", "Lite",
                                     vf.VariantOverrides(dnp=True))


def test_pcb_remove_footprint_variants(project: Path):
    pcb = _texts(project)["pcb"]
    out, count = vf.pcb_remove_footprint_variants(pcb, "Lite")
    assert count == 2  # D8 + R1
    assert vf.pcb_list_footprint_overrides(out) == {}
    # the board registry is untouched by the footprint sweep
    assert [e["name"] for e in vf.pcb_list_board_variants(out)] == [
        "Standard", "Lite"]


# ── project resolution ───────────────────────────────────────────────────────

def test_resolve_project_from_any_sibling(project: Path):
    for suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        files = vf.resolve_project(project.with_suffix(suffix))
        assert files.pro == project
        assert files.sch is not None and files.pcb is not None


def test_resolve_project_requires_pro(tmp_path: Path):
    orphan = tmp_path / "alone.kicad_sch"
    orphan.write_text("(kicad_sch (version 20260306))", encoding="utf-8")
    with pytest.raises(vf.VariantAuthorError):
        vf.resolve_project(orphan)
    with pytest.raises(vf.VariantAuthorError):
        vf.resolve_project(tmp_path / "readme.txt")


# ── write session (REQ-S4B-006/009) ──────────────────────────────────────────

def test_session_commit_nothing_staged(project: Path, fake_oracle: type[_FakeDRCOps]):
    session = vf.VariantWriteSession(vf.resolve_project(project))
    assert session.commit(FAKE_CLI) == {"changed": False, "files": []}


def test_session_commit_success_replaces_atomically(
    project: Path, fake_oracle: type[_FakeDRCOps],
):
    files = vf.resolve_project(project)
    session = vf.VariantWriteSession(files)
    staged = vf.pro_upsert_variant(session.texts[files.pro], "Pro", None)
    assert session.stage(files.pro, staged)
    result = session.commit(FAKE_CLI)
    assert result["changed"] is True
    assert files.pro.read_text(encoding="utf-8") == staged
    assert not list(project.parent.glob(".variant_write_tmp_*"))


def test_session_erc_count_change_aborts(
    project: Path, fake_oracle: type[_FakeDRCOps],
):
    """A write that changes the ERC count is rejected; originals untouched."""
    fake_oracle.erc_script = [(0, 80), (1, 80)]  # baseline, authored
    files = vf.resolve_project(project)
    before = _texts(project)
    session = vf.VariantWriteSession(files)
    assert files.sch is not None
    new_sch, _ = vf.sch_remove_variant(session.texts[files.sch], "Lite")
    session.stage(files.sch, new_sch)
    session.stage(files.pro, vf.pro_remove_variant(session.texts[files.pro], "Lite"))
    with pytest.raises(vf.VariantWriteError) as exc:
        session.commit(FAKE_CLI)
    assert exc.value.payload["error"] == "validation_failed"
    assert exc.value.payload["stage"] == "erc"
    # all-or-nothing: BOTH staged files stay byte-identical, temps cleaned
    assert _texts(project) == before
    assert not list(project.parent.glob(".variant_write_tmp_*"))


def test_session_missing_erc_report_aborts(
    project: Path, fake_oracle: type[_FakeDRCOps],
):
    fake_oracle.write_report = False
    files = vf.resolve_project(project)
    before = _texts(project)
    session = vf.VariantWriteSession(files)
    assert files.sch is not None
    new_sch, _ = vf.sch_remove_variant(session.texts[files.sch], "Lite")
    session.stage(files.sch, new_sch)
    with pytest.raises(vf.VariantWriteError):
        session.commit(FAKE_CLI)
    assert _texts(project) == before


def test_session_stale_file_aborts(project: Path, fake_oracle: type[_FakeDRCOps]):
    """A concurrent write between snapshot and commit aborts the session."""
    import os

    files = vf.resolve_project(project)
    session = vf.VariantWriteSession(files)
    session.stage(files.pro, vf.pro_upsert_variant(
        session.texts[files.pro], "Pro", None))
    # simulate an external writer touching the original
    stat = files.pro.stat()
    os.utime(files.pro, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    before = files.pro.read_text(encoding="utf-8")
    with pytest.raises(vf.VariantWriteError) as exc:
        session.commit(FAKE_CLI)
    assert exc.value.payload["error"] == "stale_file"
    assert files.pro.read_text(encoding="utf-8") == before


# ── tool bodies: gate + orchestration ────────────────────────────────────────

def test_gate_refusal_payload_and_ordering(project: Path, gate_refuse: None):
    """On KiCad 9 every mutating tool refuses BEFORE touching the project —
    even a nonsense path returns the refusal, proving the gate runs first."""
    before = _texts(project)
    for call in (
        lambda: tool_mod.run_create_variant(str(project), "X", None),
        lambda: tool_mod.run_delete_variant(str(project), "Lite"),
        lambda: tool_mod.run_set_variant_overrides(
            str(project), "Lite", "R1", dnp=True),
        lambda: tool_mod.run_create_variant("Z:/does/not/exist.kicad_pro", "X", None),
    ):
        payload = json.loads(call())
        assert payload["status"] == "error"
        assert payload["error"] == "capability_unavailable"
        assert payload["capability"] == "variant-authoring"
        assert payload["detected"] == "9.0.7"
        assert "KICAD_MCP_KICAD_VERSION" in payload["hint"]
    assert _texts(project) == before  # zero mutation (REQ-S4B-005)


def test_gate_refusal_when_cli_missing():
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=None):
        payload = json.loads(tool_mod.run_create_variant("x.kicad_pro", "X", None))
    assert payload["error"] == "capability_unavailable"
    assert payload["detected"] == "kicad-cli not found"


def test_list_variants_reads_fixture(project: Path):
    """REQ-S4B-004: the committed fixture lists exactly what it carries."""
    payload = json.loads(tool_mod.run_list_variants(str(project)))
    assert payload["status"] == "success"
    by_name = {v["name"]: v for v in payload["variants"]}
    assert set(by_name) == {"Standard", "Lite"}
    lite = by_name["Lite"]
    assert lite["in_pro_registry"] and lite["in_board_registry"]
    assert lite["components"]["D8"]["overrides"] == {"dnp": True}
    assert lite["components"]["D8"]["sides"] == ["sch", "pcb"]
    assert lite["components"]["R1"]["overrides"]["fields"] == {"Value": "4.7k"}
    assert lite["components"]["R1"]["sides"] == ["sch", "pcb"]
    assert payload["inconsistencies"] == []
    assert "components" not in by_name["Standard"]


def test_list_variants_flags_one_sided_override(project: Path):
    """An override present on only one side is surfaced, not hidden."""
    sch_path = project.with_suffix(".kicad_sch")
    sch = sch_path.read_text(encoding="utf-8")
    out = vf.sch_set_variant(sch, "R5", "Lite", vf.VariantOverrides(dnp=True))
    sch_path.write_text(out, encoding="utf-8")
    payload = json.loads(tool_mod.run_list_variants(str(project)))
    assert any("R5" in msg and "only on" in msg for msg in payload["inconsistencies"])


def test_list_variants_pre_v10_project_is_empty_not_error(tmp_path: Path):
    (tmp_path / "old.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "old.kicad_sch").write_text(
        "(kicad_sch (version 20250114))", encoding="utf-8")
    payload = json.loads(tool_mod.run_list_variants(str(tmp_path / "old.kicad_pro")))
    assert payload["status"] == "success"
    assert payload["variants"] == []


def test_create_then_override_then_delete_round_trip(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    """The full authoring loop on a scratch copy, fake oracle in place."""
    created = json.loads(tool_mod.run_create_variant(
        str(project), "Pro", "premium build"))
    assert created["status"] == "success" and created["changed"]
    assert created["changes"]["pro"] and created["changes"]["pcb"]

    set_result = json.loads(tool_mod.run_set_variant_overrides(
        str(project), "Pro", "D3", dnp=True, fields={"Value": "green"}))
    assert set_result["status"] == "success" and set_result["changed"]
    assert set_result["changes"]["sch"] and set_result["changes"]["pcb"]

    listing = json.loads(tool_mod.run_list_variants(str(project)))
    by_name = {v["name"]: v for v in listing["variants"]}
    assert by_name["Pro"]["components"]["D3"]["overrides"] == {
        "dnp": True, "fields": {"Value": "green"}}
    assert by_name["Pro"]["components"]["D3"]["sides"] == ["sch", "pcb"]
    assert listing["inconsistencies"] == []

    deleted = json.loads(tool_mod.run_delete_variant(str(project), "Pro"))
    assert deleted["status"] == "success" and deleted["changed"]
    listing = json.loads(tool_mod.run_list_variants(str(project)))
    assert {v["name"] for v in listing["variants"]} == {"Standard", "Lite"}
    assert "Pro" not in _texts(project)["sch"]
    assert "Pro" not in _texts(project)["pcb"]


def test_delete_variant_full_cleanup(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    """QS4-4: delete removes registry entries + every override, both sides."""
    result = json.loads(tool_mod.run_delete_variant(str(project), "Lite"))
    assert result["status"] == "success" and result["changed"]
    texts = _texts(project)
    assert "Lite" not in texts["pro"]
    assert "(variant" not in texts["sch"]
    fp = vf.pcb_list_footprint_overrides(texts["pcb"])
    assert fp == {}
    assert [e["name"] for e in vf.pcb_list_board_variants(texts["pcb"])] == ["Standard"]


def test_delete_absent_variant_is_noop(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    before = _texts(project)
    result = json.loads(tool_mod.run_delete_variant(str(project), "Nope"))
    assert result["status"] == "success"
    assert result["changed"] is False
    assert "nothing to delete" in result["message"]
    assert _texts(project) == before


def test_set_overrides_unknown_variant_is_parameter_error(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    before = _texts(project)
    result = json.loads(tool_mod.run_set_variant_overrides(
        str(project), "Ghost", "R1", dnp=True))
    assert result["status"] == "error"
    assert result["error"] == "parameter_error"
    assert "Standard" in result["message"] and "Lite" in result["message"]
    assert _texts(project) == before


def test_set_overrides_unknown_reference_is_parameter_error(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    result = json.loads(tool_mod.run_set_variant_overrides(
        str(project), "Lite", "R99", dnp=True))
    assert result["status"] == "error"
    assert "R99" in result["message"]


def test_create_variant_empty_name_rejected():
    result = json.loads(tool_mod.run_create_variant("x.kicad_pro", "  ", None))
    assert result["status"] == "error"
    assert result["error"] == "parameter_error"


def test_create_existing_variant_is_noop(
    project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    before = _texts(project)
    result = json.loads(tool_mod.run_create_variant(str(project), "Standard", None))
    assert result["status"] == "success"
    assert result["changed"] is False
    assert _texts(project) == before


def test_mutating_tool_refuses_pre_v10_format(
    tmp_path: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    (tmp_path / "old.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "old.kicad_sch").write_text(
        "(kicad_sch (version 20250114))", encoding="utf-8")
    before = (tmp_path / "old.kicad_sch").read_text(encoding="utf-8")
    result = json.loads(tool_mod.run_create_variant(
        str(tmp_path / "old.kicad_pro"), "X", None))
    assert result["status"] == "error"
    assert result["error"] == "format_too_old"
    assert (tmp_path / "old.kicad_sch").read_text(encoding="utf-8") == before


def test_sch_only_project_degrades_gracefully(
    tmp_path: Path, project: Path, gate_pass: None, fake_oracle: type[_FakeDRCOps],
):
    """A project with no board authors the sch+pro sides and says so."""
    sub = tmp_path / "schonly"
    sub.mkdir()
    shutil.copy2(project, sub / project.name)
    shutil.copy2(project.with_suffix(".kicad_sch"),
                 sub / project.with_suffix(".kicad_sch").name)
    pro = sub / project.name
    result = json.loads(tool_mod.run_set_variant_overrides(
        str(pro), "Lite", "R2", dnp=True))
    assert result["status"] == "success" and result["changed"]
    assert result["changes"]["pcb"] == "absent"
    assert result["changes"]["sch"] == "symbol override block updated"
