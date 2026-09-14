"""render_view — agent-side visual self-inspection (spec-render-view §R2-§R6).

Two layers, mocked at the subprocess / ops boundary per src/CLAUDE.md:
1. CLIExportOps.render_board argv construction (patched subprocess.run).
2. The render_view tool closure — dispatch, parameter validation, error
   surfacing, save-before-render — against a capturing MCP stub and a
   MagicMock backend.
The final test is a kicad-cli-gated live oracle (skipped when absent).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.backends.cli_backend import CLIExportOps
from kicad_mcp.tools import export as export_mod
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.platform_helper import find_kicad_cli

CLI = Path(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")


class _CapturingMCP:
    """Minimal stand-in for FastMCP that records @mcp.tool()-decorated fns."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _render_view_fn(backend: MagicMock, tmp_path: Path) -> Any:
    mcp = _CapturingMCP()
    change_log = ChangeLog(tmp_path / "changes.json")
    export_mod.register_tools(mcp, backend, change_log)
    return mcp.tools["render_view"]


def _pcb(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_pcb"
    p.write_text("(kicad_pcb (version 20241229))", encoding="utf-8")
    return p


def _sch(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text("(kicad_sch (version 20260306))", encoding="utf-8")
    return p


def _backend_with_ops(ops: MagicMock) -> MagicMock:
    backend = MagicMock()
    backend.get_export_ops.return_value = ops
    return backend


def _ops_creating_output(result_key: str = "render_board") -> MagicMock:
    """Ops mock whose render/export methods create the output file (so the
    tool's exists() check passes) and report success."""
    ops = MagicMock()

    def fake_render(path: Path, out: Path, **kw: Any) -> dict[str, Any]:
        out.write_bytes(b"\x89PNG fake")
        return {"success": True, "output_file": str(out), "message": "Board rendered"}

    def fake_pdf(path: Path, out: Path, layers: Any, **kw: Any) -> dict[str, Any]:
        out.write_bytes(b"%PDF fake")
        return {"success": True, "output_files": [str(out)], "message": "PDF exported"}

    ops.render_board.side_effect = fake_render
    ops.export_pdf.side_effect = fake_pdf
    return ops


_CLI_PRESENT = patch("shutil.which", return_value=str(CLI))


# ── §R3 ops layer: argv construction ─────────────────────────────────────────

def _capture_render_cmd(board: Path, out: Path, **kw: Any) -> list[str]:
    captured: dict = {}

    def fake_run(cmd: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    ops = CLIExportOps(CLI)
    with patch("subprocess.run", side_effect=fake_run):
        ops.render_board(board, out, **kw)
    return captured["cmd"]


def test_render_board_builds_expected_cli_args(tmp_path: Path) -> None:
    board = _pcb(tmp_path)
    out = tmp_path / "view.png"
    cmd = _capture_render_cmd(board, out)
    assert cmd == [
        str(CLI), "pcb", "render",
        "--output", str(out),
        "--side", "top",
        "--width", "1600",
        "--height", "900",
        "--background", "opaque",
        str(board),
    ]


def test_render_board_optional_zoom_rotate_flags(tmp_path: Path) -> None:
    board = _pcb(tmp_path)
    out = tmp_path / "view.png"
    cmd = _capture_render_cmd(
        board, out, side="bottom", width=800, height=600,
        zoom=1.5, rotate="-45,0,45",
    )
    assert cmd == [
        str(CLI), "pcb", "render",
        "--output", str(out),
        "--side", "bottom",
        "--width", "800",
        "--height", "600",
        "--background", "opaque",
        "--zoom", "1.5",
        "--rotate", "-45,0,45",
        str(board),
    ]


# ── §R2 tool layer: dispatch + validation ────────────────────────────────────

def test_render_schematic_uses_sch_export_pdf(tmp_path: Path) -> None:
    ops = _ops_creating_output()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    sch = _sch(tmp_path)
    out = tmp_path / "sch.pdf"

    with _CLI_PRESENT:
        result = json.loads(tool(path=str(sch), output=str(out)))

    assert result["status"] == "success"
    assert result["kind"] == "schematic"
    assert result["format"] == "pdf"
    assert Path(result["output_file"]) == out.resolve()
    ops.export_pdf.assert_called_once()
    ops.render_board.assert_not_called()


def test_render_view_rejects_unknown_suffix(tmp_path: Path) -> None:
    ops = MagicMock()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    pro = tmp_path / "test.kicad_pro"
    pro.write_text("{}", encoding="utf-8")

    result = json.loads(tool(path=str(pro)))

    assert result["status"] == "error"
    assert result["error"] == "parameter_error"
    assert "accepts .kicad_pcb or .kicad_sch" in result["message"]
    ops.render_board.assert_not_called()
    ops.export_pdf.assert_not_called()


def test_render_view_rejects_bad_side(tmp_path: Path) -> None:
    ops = MagicMock()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)

    result = json.loads(tool(path=str(board), side="iso"))

    assert result["status"] == "error"
    assert result["error"] == "parameter_error"
    assert "side must be one of" in result["message"]
    ops.render_board.assert_not_called()


def test_render_view_rejects_board_params_for_schematic(tmp_path: Path) -> None:
    ops = MagicMock()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    sch = _sch(tmp_path)

    result = json.loads(tool(path=str(sch), side="bottom"))

    assert result["status"] == "error"
    assert result["error"] == "parameter_error"
    assert "board renders only" in result["message"]
    ops.export_pdf.assert_not_called()


def test_render_view_rejects_out_of_range_dimensions(tmp_path: Path) -> None:
    ops = MagicMock()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)

    result = json.loads(tool(path=str(board), width=32))

    assert result["status"] == "error"
    assert result["error"] == "parameter_error"
    assert "between 64 and 8192" in result["message"]
    ops.render_board.assert_not_called()


# ── §R4 error surfacing ──────────────────────────────────────────────────────

def test_render_view_missing_cli_error(tmp_path: Path) -> None:
    backend = _backend_with_ops(MagicMock())
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)

    with patch("shutil.which", return_value=None), \
         patch("kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=None):
        result = json.loads(tool(path=str(board)))

    assert result["status"] == "error"
    assert "render_view requires kicad-cli" in result["message"]
    backend.save_board.assert_not_called()


def test_render_view_cli_failure_surfaces_stderr(tmp_path: Path) -> None:
    ops = MagicMock()
    ops.render_board.return_value = {"success": False, "message": "raytracer exploded"}
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)

    with _CLI_PRESENT:
        result = json.loads(tool(path=str(board)))

    assert result["status"] == "error"
    assert "raytracer exploded" in result["message"]
    assert "pcb render" in result["command_attempted"]


def test_render_view_missing_output_file_is_error(tmp_path: Path) -> None:
    ops = MagicMock()
    ops.render_board.return_value = {"success": True, "message": "Board rendered"}
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)
    out = tmp_path / "never_written.png"

    with _CLI_PRESENT:
        result = json.loads(tool(path=str(board), output=str(out)))

    assert result["status"] == "error"
    assert "output file was not created" in result["message"]
    assert result["output_path"] == str(out)


def test_render_view_not_implemented_backend(tmp_path: Path) -> None:
    ops = MagicMock()
    ops.render_board.side_effect = NotImplementedError("no render here")
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)

    with _CLI_PRESENT:
        result = json.loads(tool(path=str(board)))

    assert result["status"] == "error"
    assert "not implemented by the current backend" in result["message"]


# ── save-before-render + default naming ──────────────────────────────────────

def test_render_board_saves_board_first(tmp_path: Path) -> None:
    ops = _ops_creating_output()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)
    sch = _sch(tmp_path)

    with _CLI_PRESENT:
        assert json.loads(tool(path=str(board)))["status"] == "success"
        backend.save_board.assert_called_once()
        backend.save_board.reset_mock()
        assert json.loads(tool(path=str(sch)))["status"] == "success"
        backend.save_board.assert_not_called()


def test_render_view_default_output_naming(tmp_path: Path) -> None:
    ops = _ops_creating_output()
    backend = _backend_with_ops(ops)
    tool = _render_view_fn(backend, tmp_path)
    board = _pcb(tmp_path)
    sch = _sch(tmp_path)

    with _CLI_PRESENT:
        board_result = json.loads(tool(path=str(board)))
        sch_result = json.loads(tool(path=str(sch)))

    assert Path(board_result["output_file"]).name == "test_render.png"
    assert Path(sch_result["output_file"]).name == "test_render.pdf"
    assert Path(board_result["output_file"]).parent == tmp_path.resolve()


# ── live oracle (kicad-cli gated) ────────────────────────────────────────────

@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_render_board_oracle_live(tmp_path: Path) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "boards"
    boards = sorted(fixtures.glob("*.kicad_pcb"))
    assert boards, "no board fixture available"
    out = tmp_path / "oracle.png"

    ops = CLIExportOps(find_kicad_cli())
    result = ops.render_board(boards[0], out, width=320, height=180)

    assert result["success"], result["message"]
    assert out.exists()
    assert out.stat().st_size > 1024
    assert out.read_bytes()[:4] == b"\x89PNG"
