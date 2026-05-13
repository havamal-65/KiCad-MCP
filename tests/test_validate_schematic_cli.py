"""Tests for validate_schematic_cli — verifies kicad-cli arg shape (KiCad 9 svg subcommand)."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import fastmcp
import pytest

from kicad_mcp.utils.change_log import ChangeLog


MINIMAL_SCH = textwrap.dedent("""\
    (kicad_sch
      (version 20231231)
      (generator "eeschema")
    )
""")


@pytest.fixture
def tmp_sch(tmp_path: Path) -> Path:
    f = tmp_path / "test.kicad_sch"
    f.write_text(MINIMAL_SCH, encoding="utf-8")
    return f


def _call_tool(sch_path: Path, fake_run) -> dict:
    backend_stub = MagicMock()
    change_log = ChangeLog(sch_path.parent / "changes.json")
    mcp = fastmcp.FastMCP("test")
    from kicad_mcp.tools import drc
    drc.register_tools(mcp, backend_stub, change_log)
    tool_fn = next(t.fn for t in mcp._tool_manager._tools.values()
                   if t.name == "validate_schematic_cli")
    fake_cli = MagicMock(__str__=lambda s: "/usr/bin/kicad-cli")
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=fake_cli), \
         patch("subprocess.run", side_effect=fake_run):
        raw = tool_fn(str(sch_path))
    return json.loads(raw)


def test_uses_svg_subcommand_not_format_flag(tmp_sch: Path):
    """Arg list must contain positional 'svg' subcommand, not '--format svg'."""
    captured: dict = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _call_tool(tmp_sch, fake_run)

    assert "cmd" in captured, "subprocess.run was never called"
    cmd = captured["cmd"]
    # Subcommand form: ... sch export svg --output DIR INPUT
    assert "sch" in cmd
    assert "export" in cmd
    assert "svg" in cmd
    sch_idx = cmd.index("sch")
    assert cmd[sch_idx + 1] == "export"
    assert cmd[sch_idx + 2] == "svg", \
        f"Expected positional 'svg' after 'export', got {cmd[sch_idx + 2]!r}"
    # The stale form would be: ... sch export --format svg ...
    assert "--format" not in cmd, "Stale --format flag is still present"


def test_output_is_directory_not_file(tmp_sch: Path):
    """KiCad 9 sch export svg writes to OUTPUT_DIR, not a file path."""
    captured: dict = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _call_tool(tmp_sch, fake_run)

    cmd = captured["cmd"]
    out_idx = cmd.index("--output")
    out_value = cmd[out_idx + 1]
    # Output must point at a directory (no .svg suffix)
    assert not out_value.endswith(".svg"), \
        f"--output should be a directory, got file path: {out_value}"


def test_passing_run_returns_passed_true(tmp_sch: Path):
    def fake_run(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = _call_tool(tmp_sch, fake_run)
    assert result["status"] == "success"
    assert result["passed"] is True


def test_failing_run_returns_passed_false(tmp_sch: Path):
    def fake_run(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="symbol load error")
    result = _call_tool(tmp_sch, fake_run)
    assert result["passed"] is False
    assert "symbol load error" in result["message"]
