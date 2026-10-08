"""Tests for F-DV13 — a valid partial STEP export is structured partial success.

Three layers, all V-A (no KiCad):
1. ``_valid_step_file`` + ``CLIExportOps.export_step`` — exit-code + file
   evidence decides success / partial / bare failure, with the pre-run
   snapshot guarding against stale files (``_run`` is mocked; fakes write
   the output file themselves).
2. The ``export_step`` MCP tool — three-way status (``success`` / ``partial``
   / ``error``), ``excluded_models`` + ``remedy`` on partial, the bare-failure
   ``status: "error"`` tightening.
3. ``step_excluded_models`` — the best-effort classification walk
   (``vrml_only`` / ``model_missing``) and its non-fatal error handling.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import fastmcp
import pytest

from kicad_mcp.backends.cli_backend import CLIExportOps, _valid_step_file
from kicad_mcp.tools import export as export_mod
from kicad_mcp.utils.change_log import ChangeLog

CLI = Path(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")

# Measured framing (probes 2026-10-05): both the partial and the clean export
# start with the ISO-10303-21 header and end with the END trailer.
_VALID_STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
_VALID_STEP_2 = (
    b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n/* rewritten */\nENDSEC;\nEND-ISO-10303-21;\n"
)
_STDERR = "Cannot use VRML models when exporting to non-mesh formats.\n"

_REMEDY = (
    "The STEP file on disk is valid and usable; it omits the models listed in "
    "excluded_models. To get a complete export, provide a STEP/STP version of "
    "each omitted model next to its .wrl (kicad-cli --subst-models picks up "
    "same-name siblings), or accept the partial file."
)


def _run_result(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def _ops_with_run(monkeypatch: pytest.MonkeyPatch, fake_run: Any) -> CLIExportOps:
    ops = CLIExportOps(CLI)
    monkeypatch.setattr(ops, "_run", fake_run)
    return ops


# ── backend layer — exit code + file evidence ────────────────────────────────

def test_exit0_success_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"
    ops = _ops_with_run(monkeypatch, lambda args, timeout=0: _run_result(0))

    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res == {"success": True, "output_file": str(out), "message": "STEP exported"}
    assert "partial" not in res


def test_exit2_with_valid_new_file_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"

    def fake_run(args: list[str], timeout: int = 0) -> subprocess.CompletedProcess[str]:
        out.write_bytes(_VALID_STEP)
        return _run_result(2, _STDERR)

    ops = _ops_with_run(monkeypatch, fake_run)
    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res["success"] is False
    assert res["partial"] is True
    assert res["output_file"] == str(out)
    assert res["file_size_bytes"] == len(_VALID_STEP) == out.stat().st_size
    assert res["stderr"] == _STDERR
    assert res["message"] == (
        "STEP exported partially: kicad-cli exited 2 but wrote a valid STEP "
        "file; some 3D models are absent from it."
    )


def test_exit2_no_file_is_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"
    ops = _ops_with_run(monkeypatch, lambda args, timeout=0: _run_result(2, _STDERR))

    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res == {"success": False, "output_file": None, "message": _STDERR}
    assert "partial" not in res


def test_exit2_invalid_header_is_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"

    def fake_run(args: list[str], timeout: int = 0) -> subprocess.CompletedProcess[str]:
        out.write_bytes(b"NOT-A-STEP-FILE;\nEND-ISO-10303-21;\n")
        return _run_result(2, _STDERR)

    ops = _ops_with_run(monkeypatch, fake_run)
    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res["success"] is False
    assert "partial" not in res
    assert res["output_file"] is None


def test_exit2_missing_trailer_is_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"

    def fake_run(args: list[str], timeout: int = 0) -> subprocess.CompletedProcess[str]:
        # Valid header, but the write was truncated — no END trailer.
        out.write_bytes(b"ISO-10303-21;\nHEADER;\nDATA;\n/* truncated")
        return _run_result(2, _STDERR)

    ops = _ops_with_run(monkeypatch, fake_run)
    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res["success"] is False
    assert "partial" not in res
    assert res["output_file"] is None


def test_preexisting_file_unchanged_is_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"
    out.write_bytes(_VALID_STEP)  # leftover from an earlier successful export
    ops = _ops_with_run(monkeypatch, lambda args, timeout=0: _run_result(2, _STDERR))

    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res["success"] is False
    assert "partial" not in res
    assert res["output_file"] is None


def test_preexisting_file_rewritten_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "model.step"
    out.write_bytes(_VALID_STEP)

    def fake_run(args: list[str], timeout: int = 0) -> subprocess.CompletedProcess[str]:
        out.write_bytes(_VALID_STEP_2)  # size changes → written by this run
        return _run_result(2, _STDERR)

    ops = _ops_with_run(monkeypatch, fake_run)
    res = ops.export_step(tmp_path / "b.kicad_pcb", out)

    assert res["success"] is False
    assert res["partial"] is True
    assert res["output_file"] == str(out)
    assert res["file_size_bytes"] == len(_VALID_STEP_2)


def test_valid_step_file_accepts_probe_framing(tmp_path: Path):
    good = tmp_path / "good.step"
    good.write_bytes(_VALID_STEP)
    assert _valid_step_file(good) is True

    empty = tmp_path / "empty.step"
    empty.write_bytes(b"")
    assert _valid_step_file(empty) is False


# ── tool layer — three-way status ────────────────────────────────────────────

_PARTIAL_PAYLOAD = {
    "success": False,
    "partial": True,
    "output_file": "model.step",
    "file_size_bytes": 60578,
    "stderr": _STDERR,
    "message": (
        "STEP exported partially: kicad-cli exited 2 but wrote a valid STEP "
        "file; some 3D models are absent from it."
    ),
}


def _board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    return p


def _export_step_tool(
    tmp_path: Path, backend_result: dict[str, Any],
) -> tuple[Any, ChangeLog]:
    backend = MagicMock()
    backend.get_export_ops.return_value.export_step.return_value = backend_result
    mcp = fastmcp.FastMCP("test")
    change_log = ChangeLog(tmp_path / "changes.json")
    export_mod.register_tools(mcp, backend, change_log)
    tool_fn = next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "export_step"
    )
    return tool_fn, change_log


def test_tool_export_step_partial_status(tmp_path: Path):
    tool_fn, change_log = _export_step_tool(tmp_path, dict(_PARTIAL_PAYLOAD))

    parsed = json.loads(tool_fn(str(_board(tmp_path)), str(tmp_path / "model.step")))

    assert parsed["status"] == "partial"
    assert parsed["partial"] is True
    assert parsed["excluded_models"] == []  # minimal board carries no footprints
    assert parsed["remedy"] == _REMEDY
    assert parsed["stderr"] == _STDERR
    recorded = change_log.get_recent()
    assert any(e["tool"] == "export_step" for e in recorded)


def test_tool_export_step_bare_failure_status_error(tmp_path: Path):
    tool_fn, _ = _export_step_tool(
        tmp_path, {"success": False, "output_file": None, "message": "Failed to load board"},
    )

    parsed = json.loads(tool_fn(str(_board(tmp_path)), str(tmp_path / "model.step")))

    assert parsed["status"] == "error"
    assert parsed["message"] == "Failed to load board"


# ── step_excluded_models — classification walk ───────────────────────────────

def _footprint(ref: str, lib_id: str, models: list[str]) -> str:
    model_clauses = "\n".join(
        f'    (model "{m}" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))'
        for m in models
    )
    return (
        f'  (footprint "{lib_id}"\n'
        f'    (layer "F.Cu")\n'
        f'    (at 10 10)\n'
        f'    (property "Reference" "{ref}" (at 0 0 0))\n'
        f'    (property "Value" "{ref}val" (at 0 0 0))\n'
        f"{model_clauses}\n"
        f"  )"
    )


def test_step_excluded_models_classifies(tmp_path: Path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "u1.wrl").write_text("#VRML V2.0 utf8", encoding="utf-8")  # no STEP sibling
    (models / "u3.step").write_text("ISO-10303-21; /* stub */", encoding="utf-8")
    body = "\n".join([
        _footprint("U1", "Lib:A", ["${KIPRJMOD}/models/u1.wrl"]),
        _footprint("U2", "Lib:B", ["${NOPE_MODEL_DIR}/x.step"]),
        _footprint("U3", "Lib:C", ["${KIPRJMOD}/models/u3.step"]),
    ])
    board = tmp_path / "b.kicad_pcb"
    board.write_text(
        f"(kicad_pcb (version 20240101) (generator test)\n{body}\n)\n", encoding="utf-8",
    )

    excluded = export_mod.step_excluded_models(board)

    by_ref = {e["ref"]: e for e in excluded}
    assert set(by_ref) == {"U1", "U2"}  # the resolving .step (U3) is not listed
    assert by_ref["U1"]["reason"] == "vrml_only"
    assert by_ref["U1"]["model_path"] == "${KIPRJMOD}/models/u1.wrl"
    assert by_ref["U1"]["footprint"] == "Lib:A"
    assert by_ref["U2"]["reason"] == "model_missing"
    assert by_ref["U2"]["model_path"] == "${NOPE_MODEL_DIR}/x.step"


def test_step_excluded_models_walk_error_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    def _boom(board_path: Path) -> list[dict[str, Any]]:
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(export_mod, "step_excluded_models", _boom)
    tool_fn, _ = _export_step_tool(tmp_path, dict(_PARTIAL_PAYLOAD))

    parsed = json.loads(tool_fn(str(_board(tmp_path)), str(tmp_path / "model.step")))

    assert parsed["status"] == "partial"
    assert parsed["excluded_models"] == []
    assert "walk exploded" in parsed["excluded_models_error"]
