"""Tests for CLIExportOps.export_pdf — verifies single comma-joined --layers flag."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.backends.cli_backend import CLIExportOps


def _capture_args(cli_path: Path, board: Path, output: Path, layers):
    captured: dict = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    ops = CLIExportOps(cli_path)
    with patch("subprocess.run", side_effect=fake_run):
        ops.export_pdf(board, output, layers=layers)
    return captured["cmd"]


def test_layers_joined_with_comma_single_flag(tmp_path: Path):
    """Multiple layers must produce ONE --layers flag with comma-separated value."""
    cli = Path("/usr/bin/kicad-cli")
    board = tmp_path / "test.kicad_pcb"
    board.write_text("(kicad_pcb (version 20231231))", encoding="utf-8")
    output = tmp_path / "out.pdf"

    cmd = _capture_args(cli, board, output, layers=["F.Cu", "B.Cu", "Edge.Cuts"])

    # Exactly one --layers flag, value is comma-joined
    layers_indices = [i for i, tok in enumerate(cmd) if tok == "--layers"]
    assert len(layers_indices) == 1, \
        f"Expected exactly one --layers flag, got {len(layers_indices)}: {cmd}"
    layer_value = cmd[layers_indices[0] + 1]
    assert layer_value == "F.Cu,B.Cu,Edge.Cuts", \
        f"Expected comma-joined layer list, got {layer_value!r}"


def test_no_layers_means_no_flag(tmp_path: Path):
    """No layers → --layers should be absent (kicad-cli will use its defaults)."""
    cli = Path("/usr/bin/kicad-cli")
    board = tmp_path / "test.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    output = tmp_path / "out.pdf"

    cmd = _capture_args(cli, board, output, layers=None)
    assert "--layers" not in cmd


def test_schematic_path_does_not_get_layers(tmp_path: Path):
    """Sch PDF branch must not pass --layers even if caller provided some."""
    cli = Path("/usr/bin/kicad-cli")
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    output = tmp_path / "out.pdf"

    cmd = _capture_args(cli, sch, output, layers=["F.Cu", "B.Cu"])
    assert "--layers" not in cmd
    assert "sch" in cmd
    assert "pcb" not in cmd


def test_pcb_branch_uses_pcb_subcommand(tmp_path: Path):
    cli = Path("/usr/bin/kicad-cli")
    board = tmp_path / "x.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    output = tmp_path / "x.pdf"

    cmd = _capture_args(cli, board, output, layers=["F.Cu"])
    pcb_idx = cmd.index("pcb")
    assert cmd[pcb_idx + 1] == "export"
    assert cmd[pcb_idx + 2] == "pdf"
