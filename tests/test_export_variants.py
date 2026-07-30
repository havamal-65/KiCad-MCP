"""K10-S3 Feature A units — variant-aware exports (REQ-S3A-001..005).

Three layers, all V-A (no KiCad):
1. CLIExportOps argv construction — every param combination maps to the exact
   expected kicad-cli flags, including the variant=None → no-flag guarantee.
2. The tool-layer gate helper (`_variant_gate`) — ${VARIANT} validation runs
   before any subprocess; capability refusals surface the structured payload.
3. Audit plumb-through — `run_manufacturing_readiness_audit(variant=...)`
   passes the variant to the variant-capable legs, nests artifacts under
   <output_dir>/<variant>/, and reports the variant field.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.cli_backend import (
    CLIExportOps,
    _expected_outputs,
    _variant_flags,
)
from kicad_mcp.models.errors import ExportError
from kicad_mcp.tools import export as export_mod
from kicad_mcp.tools.manufacturing import run_manufacturing_readiness_audit
from kicad_mcp.utils import kicad_capabilities as kc

CLI = Path(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")


@pytest.fixture(autouse=True)
def _fresh_capability_cache() -> None:
    kc.clear_capability_cache()


def _capture_cmds(fn: Any) -> list[list[str]]:
    """Run *fn* with subprocess.run mocked; return every argv it launched."""
    cmds: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        fn()
    return cmds


# ── helpers under test ───────────────────────────────────────────────────────

def test_variant_flags_shapes():
    assert _variant_flags(None) == []
    assert _variant_flags("Standard") == ["--variant", "Standard"]
    assert _variant_flags(["Standard", "Lite"]) == [
        "--variant", "Standard", "--variant", "Lite",
    ]


def test_expected_outputs_expansion(tmp_path: Path):
    plain = tmp_path / "bom.csv"
    templated = tmp_path / "bom_${VARIANT}.csv"
    assert _expected_outputs(plain, None) == [plain]
    # variant set but no template in the path → the literal path
    assert _expected_outputs(plain, "Lite") == [plain]
    # template expands for single and list variants alike
    assert _expected_outputs(templated, "Lite") == [tmp_path / "bom_Lite.csv"]
    assert _expected_outputs(templated, ["Standard", "Lite"]) == [
        tmp_path / "bom_Standard.csv", tmp_path / "bom_Lite.csv",
    ]


# ── BOM argv construction ────────────────────────────────────────────────────

def _sch(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text("(kicad_sch (version 20260306))", encoding="utf-8")
    return p


def _pcb(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_pcb"
    p.write_text("(kicad_pcb (version 20241229))", encoding="utf-8")
    return p


def test_bom_no_variant_argv_unchanged(tmp_path: Path):
    """REQ-S3A-001: variant=None must be byte-identical to pre-variant argv."""
    sch = _sch(tmp_path)
    out = tmp_path / "bom.csv"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_bom(sch, out))
    assert cmds == [[str(CLI), "sch", "export", "bom", "--output", str(out), str(sch)]]


def test_bom_single_variant(tmp_path: Path):
    sch = _sch(tmp_path)
    out = tmp_path / "bom.csv"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_bom(sch, out, variant="Lite"))
    assert cmds == [[str(CLI), "sch", "export", "bom", "--output", str(out),
                     "--variant", "Lite", str(sch)]]


def test_bom_exclude_dnp(tmp_path: Path):
    sch = _sch(tmp_path)
    out = tmp_path / "bom.csv"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_bom(sch, out, variant="Lite", exclude_dnp=True))
    assert "--exclude-dnp" in cmds[0]
    # and without the flag when False (default)
    cmds = _capture_cmds(lambda: ops.export_bom(sch, out, variant="Lite"))
    assert "--exclude-dnp" not in cmds[0]


def test_bom_multi_variant_one_call_per_variant(tmp_path: Path):
    """A variant list runs one single---variant CLI call per name with the
    ${VARIANT} template expanded client-side: kicad-cli 10.0.4's own
    multi-variant loop writes default-variant content into every file
    (upstream defect, verified live 2026-07-21)."""
    sch = _sch(tmp_path)
    out = tmp_path / "bom_${VARIANT}.csv"
    ops = CLIExportOps(CLI)
    result: dict[str, Any] = {}
    cmds = _capture_cmds(lambda: result.update(
        ops.export_bom(sch, out, variant=["Standard", "Lite"])))
    assert len(cmds) == 2
    for cmd, name in zip(cmds, ["Standard", "Lite"]):
        assert cmd.count("--variant") == 1
        assert cmd[cmd.index("--variant") + 1] == name
        assert cmd[cmd.index("--output") + 1] == str(tmp_path / f"bom_{name}.csv")
        assert "${VARIANT}" not in " ".join(cmd)  # never hand the CLI the template
    assert result["output_files"] == [
        str(tmp_path / "bom_Standard.csv"), str(tmp_path / "bom_Lite.csv"),
    ]


# ── pick-and-place argv construction ─────────────────────────────────────────

def test_pos_no_variant_argv_unchanged(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out = tmp_path / "pos.csv"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_pick_and_place(pcb, out))
    assert cmds == [[str(CLI), "pcb", "export", "pos", "--output", str(out),
                     "--format", "csv", "--units", "mm", str(pcb)]]


def test_pos_variant_and_exclude_dnp(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out = tmp_path / "pos.csv"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_pick_and_place(
        pcb, out, variant="Lite", exclude_dnp=True))
    cmd = cmds[0]
    assert cmd[cmd.index("--variant") + 1] == "Lite"
    assert "--exclude-dnp" in cmd
    assert cmd[-1] == str(pcb)  # input stays the last token


# ── gerbers argv construction ────────────────────────────────────────────────

def test_gerbers_no_variant_argv_unchanged(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out_dir = tmp_path / "gerbers"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_gerbers(pcb, out_dir))
    assert cmds[0] == [str(CLI), "pcb", "export", "gerbers",
                       "--output", str(out_dir) + "/", str(pcb)]
    # the companion drill export runs, without any variant flag
    assert cmds[1][:4] == [str(CLI), "pcb", "export", "drill"]
    assert "--variant" not in cmds[1]


def test_gerbers_variant_flag_on_gerbers_not_drill(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out_dir = tmp_path / "gerbers"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_gerbers(pcb, out_dir, variant="Lite"))
    assert "--variant" in cmds[0]
    assert "--variant" not in cmds[1]  # drill has no variant surface


def test_gerbers_multi_variant_mkdirs_expanded_and_drills_each(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out_dir = tmp_path / "gb_${VARIANT}"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_gerbers(
        pcb, out_dir, variant=["Standard", "Lite"]))
    # expanded dirs created; the literal ${VARIANT} dir never is
    assert (tmp_path / "gb_Standard").is_dir()
    assert (tmp_path / "gb_Lite").is_dir()
    assert not (tmp_path / "gb_${VARIANT}").exists()
    # one gerber call per variant (client-side loop) + one drill call per dir
    gerber_cmds = [c for c in cmds if c[3] == "gerbers"]
    drill_cmds = [c for c in cmds if c[3] == "drill"]
    assert len(gerber_cmds) == 2
    for cmd, name in zip(gerber_cmds, ["Standard", "Lite"]):
        assert cmd.count("--variant") == 1
        assert cmd[cmd.index("--variant") + 1] == name
        assert cmd[cmd.index("--output") + 1] == str(tmp_path / f"gb_{name}") + "/"
    assert len(drill_cmds) == 2
    drill_outs = {c[c.index("--output") + 1] for c in drill_cmds}
    assert drill_outs == {str(tmp_path / "gb_Standard") + "/",
                          str(tmp_path / "gb_Lite") + "/"}


# ── PDF argv construction ────────────────────────────────────────────────────

def test_pdf_sch_variant(tmp_path: Path):
    sch = _sch(tmp_path)
    out = tmp_path / "out.pdf"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_pdf(sch, out, variant="Lite"))
    cmd = cmds[0]
    assert cmd[1] == "sch"
    assert cmd[cmd.index("--variant") + 1] == "Lite"


def test_pdf_board_with_variant_raises(tmp_path: Path):
    pcb = _pcb(tmp_path)
    out = tmp_path / "out.pdf"
    ops = CLIExportOps(CLI)
    with pytest.raises(ExportError, match="schematic-only"):
        _capture_cmds(lambda: ops.export_pdf(pcb, out, variant="Lite"))


def test_pdf_no_variant_argv_unchanged(tmp_path: Path):
    sch = _sch(tmp_path)
    out = tmp_path / "out.pdf"
    ops = CLIExportOps(CLI)
    cmds = _capture_cmds(lambda: ops.export_pdf(sch, out))
    assert cmds == [[str(CLI), "sch", "export", "pdf", "--output", str(out), str(sch)]]


# ── tool-layer gate helper (REQ-S3F-002/003, REQ-S3A-003) ────────────────────

class _OpsWithCli:
    def __init__(self, cli: Path) -> None:
        self._cli = cli


def test_gate_none_bypasses_everything():
    # No subprocess, no probe — variant=None calls must work on any KiCad.
    with patch.object(kc, "cli_supports", side_effect=AssertionError("probed!")):
        assert export_mod._variant_gate(_OpsWithCli(CLI), None, "out.csv") is None


def test_gate_empty_list_is_parameter_error():
    err = export_mod._variant_gate(_OpsWithCli(CLI), [], "out.csv")
    assert err is not None
    payload = json.loads(err)
    assert payload["error"] == "parameter_error"


def test_gate_list_without_placeholder_refuses_before_probe():
    # Validation must run BEFORE any subprocess/probe (REQ-S3A-003).
    with patch.object(kc, "cli_supports", side_effect=AssertionError("probed!")):
        err = export_mod._variant_gate(_OpsWithCli(CLI), ["Standard", "Lite"], "out/bom.csv")
    assert err is not None
    payload = json.loads(err)
    assert payload["error"] == "parameter_error"
    assert "${VARIANT}" in payload["message"]


def test_gate_keys_on_ops_cli_path(monkeypatch: pytest.MonkeyPatch):
    # REQ-S3F-002: the gate must ask about the ops' own resolved binary.
    seen: list[Path] = []

    def fake_supports(capability: str, cli_path: Path) -> bool:
        seen.append(cli_path)
        return True

    monkeypatch.setattr(kc, "cli_supports", fake_supports)
    assert export_mod._variant_gate(_OpsWithCli(CLI), "Lite", "out.csv") is None
    assert seen == [CLI]


def test_gate_refusal_surfaces_structured_payload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kc, "cli_supports", lambda c, p: False)
    monkeypatch.setattr(kc, "cli_version_echo", lambda p: "9.0.7")
    err = export_mod._variant_gate(_OpsWithCli(CLI), "Lite", "out.csv")
    assert err is not None
    payload = json.loads(err)
    assert payload["status"] == "error"
    assert payload["error"] == "capability_unavailable"
    assert payload["capability"] == "variant-exports"
    assert payload["required"] == "kicad-cli 10.0+ (--variant)"
    assert payload["detected"] == "9.0.7"
    assert "KICAD_MCP_KICAD_VERSION" in payload["hint"]


def test_gate_list_with_placeholder_passes_when_supported(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kc, "cli_supports", lambda c, p: True)
    assert export_mod._variant_gate(
        _OpsWithCli(CLI), ["Standard", "Lite"], "out/bom_${VARIANT}.csv") is None


# ── fixture integrity (REQ-S3A-004) ──────────────────────────────────────────
# The committed variant fixture was authored headlessly (pcbnew 10 API for the
# board; direct format authoring for sch/pro, grammar taken from KiCad 10
# source) and validated live by kicad-cli 10.0.4: per-variant BOM/pos deltas
# and an unchanged ERC count. These units pin the tokens so a regenerated or
# hand-touched fixture can't silently lose them.

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projects" / "variants"


def test_fixture_project_declares_variants():
    pro = json.loads((_FIXTURE_DIR / "shift_reg_v1.kicad_pro").read_text(encoding="utf-8"))
    names = [v["name"] for v in pro["schematic"]["variants"]]
    assert names == ["Standard", "Lite"]


def test_fixture_schematic_carries_variant_overrides():
    sch = (_FIXTURE_DIR / "shift_reg_v1.kicad_sch").read_text(encoding="utf-8")
    assert "(version 20260306)" in sch  # KiCad 10 format
    # D8: DNP in Lite; R1: Value override in Lite — inside symbol instances
    assert sch.count('(name "Lite")') == 2
    assert "(dnp yes)" in sch
    assert '(value "4.7k")' in sch


def test_fixture_board_carries_variants():
    pcb = (_FIXTURE_DIR / "shift_reg_v1.kicad_pcb").read_text(encoding="utf-8")
    assert "(version 20260206)" in pcb  # KiCad 10 format
    assert "(variants" in pcb  # board-level variant declarations
    # footprint-level overrides: D8 dnp + R1 field, each named Lite
    assert pcb.count('(variant\n') + pcb.count('(variant ') >= 4  # 2 board-level + 2 footprint
    assert '(value "4.7k")' in pcb


# ── audit plumb-through (REQ-S3A-002) ────────────────────────────────────────

_DRC_PASS = {"passed": True, "error_count": 0, "warning_count": 0, "violations": []}
_BS_PASS = {"passed": True, "shortfall_breakdown": {}, "warnings": []}
_CO_PASS = {"passed": True, "overlaps": []}
_M3D_PASS = {"ready": True, "checked": 1, "missing": [], "warnings": []}


def _audit_backend() -> MagicMock:
    backend = MagicMock()
    backend.get_drc_ops.return_value.run_drc.return_value = _DRC_PASS
    ops = MagicMock()
    ops.export_gerbers.return_value = {"success": True, "output_dir": "g", "output_files": ["a.gbr"]}
    ops.export_drill.return_value = {"success": True, "output_dir": "d", "output_files": ["a.drl"]}
    ops.export_bom.return_value = {"success": True, "output_files": ["bom.csv"]}
    ops.export_pick_and_place.return_value = {"success": True, "output_files": ["pos.csv"]}
    ops.export_step.return_value = {"success": True, "output_file": "model.step"}
    backend.get_export_ops.return_value = ops
    return backend


def _patch_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from kicad_mcp.tools import board as board_mod
    from kicad_mcp.tools import drc as drc_mod
    monkeypatch.setattr(board_mod, "run_verify_board_size", lambda p, **k: _BS_PASS)
    monkeypatch.setattr(drc_mod, "run_check_courtyard_overlaps", lambda p: _CO_PASS)
    monkeypatch.setattr(export_mod, "run_verify_3d_models", lambda p: _M3D_PASS)


def _audit_board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    return p


def test_audit_variant_none_reports_null_and_flat_dir(tmp_path: Path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _audit_backend()
    out = tmp_path / "out"
    result = run_manufacturing_readiness_audit(backend, _audit_board(tmp_path), out)
    assert result["variant"] is None
    ops = backend.get_export_ops.return_value
    # variant=None passed through; artifacts in output_dir itself
    assert ops.export_bom.call_args.kwargs["variant"] is None
    assert ops.export_gerbers.call_args.args[1] == out / "gerbers"


def test_audit_variant_nests_artifacts_and_passes_variant(tmp_path: Path, monkeypatch):
    _patch_checks(monkeypatch)
    backend = _audit_backend()
    out = tmp_path / "out"
    result = run_manufacturing_readiness_audit(
        backend, _audit_board(tmp_path), out, variant="Lite")
    assert result["variant"] == "Lite"
    assert result["ready_to_ship"] is True
    ops = backend.get_export_ops.return_value
    vdir = out / "Lite"
    assert ops.export_gerbers.call_args.args[1] == vdir / "gerbers"
    assert ops.export_gerbers.call_args.kwargs["variant"] == "Lite"
    assert ops.export_bom.call_args.args[1] == vdir / "bom.csv"
    assert ops.export_bom.call_args.kwargs["variant"] == "Lite"
    assert ops.export_pick_and_place.call_args.args[1] == vdir / "pos.csv"
    assert ops.export_pick_and_place.call_args.kwargs["variant"] == "Lite"
    # drill/step have no variant kwarg but still land in the variant dir
    assert ops.export_drill.call_args.args[1] == vdir / "drill"
    assert ops.export_step.call_args.args[1] == vdir / "model.step"
    assert "variant" not in ops.export_drill.call_args.kwargs
    assert "variant" not in ops.export_step.call_args.kwargs
