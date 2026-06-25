"""Tests for §6.6 verify_3d_models.

Read-only tool: walks every placed footprint's (model "…") clause, expands
KiCad path variables, and checks the file exists on disk (with a .wrl<->.step
sibling fallback). Boards + model stub files are generated in tmp_path so the
tests need no KiCad install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.export import run_verify_3d_models


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


def _board(tmp_path: Path, footprints: list[str], name: str = "b.kicad_pcb") -> Path:
    body = "\n".join(footprints)
    text = f"(kicad_pcb (version 20240101) (generator test)\n{body}\n)\n"
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _stub_model(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ISO-10303-21; /* stub */", encoding="utf-8")


# ── REQ-TEST-001 — happy path ────────────────────────────────────────────────

def test_all_models_present(tmp_path: Path):
    _stub_model(tmp_path / "models" / "u1.step")
    _stub_model(tmp_path / "models" / "u2.step")
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${KIPRJMOD}/models/u1.step"]),
        _footprint("U2", "Lib:B", ["${KIPRJMOD}/models/u2.step"]),
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is True, result
    assert result["checked"] == 2
    assert result["missing"] == []


# ── REQ-TEST-002 — missing file ──────────────────────────────────────────────

def test_missing_model_file(tmp_path: Path):
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${KIPRJMOD}/models/gone.step"]),
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is False, result
    assert len(result["missing"]) == 1
    m = result["missing"][0]
    assert m["reason"] == "file_not_found"
    assert m["ref"] == "U1"
    assert m["model_path"] == "${KIPRJMOD}/models/gone.step"
    assert m["resolved_path"].endswith("gone.step")


# ── REQ-TEST-003 — unresolvable variable ─────────────────────────────────────

def test_unresolved_variable(tmp_path: Path):
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${BOGUS_VAR}/x.step"]),
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is False, result
    m = result["missing"][0]
    assert m["reason"] == "unresolved_variable"
    assert m["variable"] == "BOGUS_VAR"


# ── REQ-TEST-004 — .wrl <-> .step sibling fallback ───────────────────────────

def test_extension_sibling_fallback(tmp_path: Path):
    # Reference a .wrl that doesn't exist; only the .step sibling is on disk.
    _stub_model(tmp_path / "models" / "u1.step")
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${KIPRJMOD}/models/u1.wrl"]),
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is True, result
    assert result["missing"] == []
    sibs = [w for w in result["warnings"] if w["type"] == "extension_sibling"]
    assert len(sibs) == 1
    assert sibs[0]["ref"] == "U1"


# ── REQ-TEST-005 — footprint with no model is skipped ────────────────────────

def test_footprint_without_model_skipped(tmp_path: Path):
    _stub_model(tmp_path / "models" / "u1.step")
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${KIPRJMOD}/models/u1.step"]),
        _footprint("J1", "Lib:Conn", []),  # THT connector, no 3D model
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is True, result
    assert result["checked"] == 1  # only U1's model counted, not J1
    assert all(m["ref"] != "J1" for m in result["missing"])


# ── REQ-TEST-006 — ${KICAD9_3DMODEL_DIR} resolution ──────────────────────────

def test_kicad9_3dmodel_dir_env_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Env-var path: ${KICAD9_3DMODEL_DIR} from the environment resolves."""
    model_root = tmp_path / "sys3d"
    _stub_model(model_root / "pkg.3dshapes" / "chip.step")
    monkeypatch.setenv("KICAD9_3DMODEL_DIR", str(model_root))
    board = _board(tmp_path, [
        _footprint("U1", "Lib:A", ["${KICAD9_3DMODEL_DIR}/pkg.3dshapes/chip.step"]),
    ])

    result = run_verify_3d_models(board)

    assert result["ready"] is True, result


def test_default_var_value_resolves_3dmodel_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Built-in default: _default_var_value finds the system 3dmodels dir
    (REQ-RESOLVE-002) when no env var is set."""
    from kicad_mcp.utils import fp_lib_table

    models = tmp_path / "3dmodels"
    models.mkdir()
    monkeypatch.delenv("KICAD9_3DMODEL_DIR", raising=False)
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths",
        lambda: [models],
    )
    fp_lib_table._default_var_value.cache_clear()
    try:
        assert fp_lib_table._default_var_value("KICAD9_3DMODEL_DIR") == str(models)
    finally:
        fp_lib_table._default_var_value.cache_clear()
