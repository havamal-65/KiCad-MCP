"""F6 — generate_netlist falls back to kicad-cli when the schematic backend punts.

The schematic-ops backend can't produce a netlist (KiCad 9 has no eeschema
Python API), so the tool must run ``kicad-cli sch export netlist`` when kicad-cli
is on PATH, and only punt when it isn't.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fastmcp

from kicad_mcp.utils.change_log import ChangeLog


def _tool(backend: MagicMock, tmp_path: Path):
    from kicad_mcp.tools import schematic
    mcp = fastmcp.FastMCP("test")
    schematic.register_tools(mcp, backend, ChangeLog(tmp_path / "changes.json"))
    return next(
        t.fn for t in mcp._tool_manager._tools.values() if t.name == "generate_netlist"
    )


def _sch(tmp_path: Path) -> Path:
    p = tmp_path / "x.kicad_sch"
    p.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    return p


def _punting_backend() -> MagicMock:
    backend = MagicMock()
    backend.get_schematic_ops.return_value.generate_netlist.side_effect = (
        NotImplementedError
    )
    return backend


def test_generate_netlist_uses_cli_when_backend_punts(tmp_path: Path) -> None:
    p = _sch(tmp_path)
    out = tmp_path / "x.net"
    tool = _tool(_punting_backend(), tmp_path)

    def fake_run(self, args, timeout=60):  # type: ignore[no-untyped-def]
        # kicad-cli would write the netlist to the --output path.
        outfile = args[args.index("--output") + 1]
        Path(outfile).write_text("(export (version D))", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch(
        "kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=Path("kicad-cli")
    ), patch("kicad_mcp.backends.cli_backend.CLIDRCOps._run", fake_run):
        res = json.loads(tool(str(p), str(out)))

    assert res["status"] == "success", res
    assert res["generated"] is True
    assert out.exists()


def test_generate_netlist_reports_error_when_cli_writes_nothing(tmp_path: Path) -> None:
    p = _sch(tmp_path)
    tool = _tool(_punting_backend(), tmp_path)

    def fake_run(self, args, timeout=60):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout="", stderr="")  # no file written

    with patch(
        "kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=Path("kicad-cli")
    ), patch("kicad_mcp.backends.cli_backend.CLIDRCOps._run", fake_run):
        res = json.loads(tool(str(p), str(tmp_path / "x.net")))

    assert res["status"] == "error"
    assert res["generated"] is False


def test_generate_netlist_punts_without_cli(tmp_path: Path) -> None:
    p = _sch(tmp_path)
    tool = _tool(_punting_backend(), tmp_path)

    with patch(
        "kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=None
    ):
        res = json.loads(tool(str(p), str(tmp_path / "x.net")))

    assert res["status"] == "info"
    assert "not found on PATH" in res["message"]
