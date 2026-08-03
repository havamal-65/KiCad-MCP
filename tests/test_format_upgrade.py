"""FB (KIR3) — KiCad-10 project format: upgrade engine, tool, and create_project option.

Mocks at the kicad-cli boundary (``_run_upgrade``) and the capability gate, per the
"mock at the CLI/file boundary" rule — no KiCad install required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import fastmcp

from kicad_mcp.tools import format_upgrade as fu
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.variant_files import file_format_version, resolve_project

SCH_V9, PCB_V9 = 20250114, 20241229
SCH_V10, PCB_V10 = 20260306, 20260206


def _v9_project(tmp_path: Path, stem: str = "proj") -> Path:
    (tmp_path / f"{stem}.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / f"{stem}.kicad_sch").write_text(
        f"(kicad_sch (version {SCH_V9}))", encoding="utf-8")
    (tmp_path / f"{stem}.kicad_pcb").write_text(
        f"(kicad_pcb (version {PCB_V9}))", encoding="utf-8")
    return tmp_path / f"{stem}.kicad_pro"


def _fake_upgrade(cli, subcmd, path):
    """Stand-in for kicad-cli: rewrite the file with a v10 stamp, succeed."""
    stamp = SCH_V10 if subcmd == "sch" else PCB_V10
    kind = "kicad_sch" if subcmd == "sch" else "kicad_pcb"
    path.write_text(f"({kind} (version {stamp}))", encoding="utf-8")
    return 0


def _stamp(p: Path) -> int | None:
    return file_format_version(p.read_text(encoding="utf-8"))


# ── upgrade engine / tool ────────────────────────────────────────────────────

def test_upgrade_no_v10_cli_refuses(tmp_path: Path):
    pro = _v9_project(tmp_path)
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=None):
        res = json.loads(fu.run_upgrade_project_format(str(pro)))
    assert res["error"] == "capability_unavailable"
    # Nothing touched.
    assert _stamp(tmp_path / "proj.kicad_sch") == SCH_V9
    assert not (tmp_path / ".kicad_mcp_backups").exists()


def test_upgrade_already_v10_noop(tmp_path: Path):
    pro = _v9_project(tmp_path)
    (tmp_path / "proj.kicad_sch").write_text(f"(kicad_sch (version {SCH_V10}))", encoding="utf-8")
    (tmp_path / "proj.kicad_pcb").write_text(f"(kicad_pcb (version {PCB_V10}))", encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("kicad-cli should not run for an already-v10 project")

    with patch.object(fu, "v10_cli_gate", return_value=(Path("kicad-cli"), None)), \
         patch.object(fu, "_run_upgrade", _boom):
        res = json.loads(fu.run_upgrade_project_format(str(pro)))
    assert res["status"] == "success"
    assert res["changed"] is False
    assert not (tmp_path / ".kicad_mcp_backups").exists()


def test_upgrade_happy_path(tmp_path: Path):
    pro = _v9_project(tmp_path)
    with patch.object(fu, "v10_cli_gate", return_value=(Path("kicad-cli"), None)), \
         patch.object(fu, "_run_upgrade", _fake_upgrade):
        res = json.loads(fu.run_upgrade_project_format(str(pro)))

    assert res["status"] == "success" and res["changed"] is True
    assert _stamp(tmp_path / "proj.kicad_sch") == SCH_V10
    assert _stamp(tmp_path / "proj.kicad_pcb") == PCB_V10
    # Backups of both changed files exist and report before/after.
    backups = list((tmp_path / ".kicad_mcp_backups").glob("*"))
    assert len(backups) == 2
    befores = {f["before"] for f in res["files"]}
    assert befores == {SCH_V9, PCB_V9}


def test_upgrade_cli_failure_restores_backup(tmp_path: Path):
    pro = _v9_project(tmp_path)

    def _fail(cli, subcmd, path):  # returns nonzero, leaves file unchanged
        return 1

    with patch.object(fu, "v10_cli_gate", return_value=(Path("kicad-cli"), None)), \
         patch.object(fu, "_run_upgrade", _fail):
        res = json.loads(fu.run_upgrade_project_format(str(pro)))

    assert res["status"] == "error"
    assert res["stage"] in ("sch_upgrade", "pcb_upgrade")
    # Original restored from backup (still v9).
    assert _stamp(tmp_path / "proj.kicad_sch") == SCH_V9
    assert _stamp(tmp_path / "proj.kicad_pcb") == PCB_V9


def test_upgrade_not_a_project(tmp_path: Path):
    stray = tmp_path / "loose.kicad_sch"
    stray.write_text(f"(kicad_sch (version {SCH_V9}))", encoding="utf-8")
    res = json.loads(fu.run_upgrade_project_format(str(stray)))
    assert res["error"] == "not_a_project"


# ── create_project(target_format=...) ────────────────────────────────────────

def _create_project_fn(tmp_path: Path):
    from kicad_mcp.tools import project
    from unittest.mock import MagicMock
    mcp = fastmcp.FastMCP("test")
    project.register_tools(mcp, MagicMock(), ChangeLog(tmp_path / "changes.json"))
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == "create_project")


def test_create_project_default_stays_v9(tmp_path: Path):
    fn = _create_project_fn(tmp_path)
    res = json.loads(fn(str(tmp_path / "d9"), "d9", design_rules_preset=""))
    assert res["project"]["target_format"] == "kicad9"
    assert "format_upgrade" not in res["project"]
    assert _stamp(tmp_path / "d9" / "d9.kicad_sch") == SCH_V9


def test_create_project_kicad10_upgrades(tmp_path: Path):
    fn = _create_project_fn(tmp_path)
    with patch.object(fu, "v10_cli_gate", return_value=(Path("kicad-cli"), None)), \
         patch.object(fu, "_run_upgrade", _fake_upgrade):
        res = json.loads(fn(str(tmp_path / "d10"), "d10", design_rules_preset="", target_format="kicad10"))

    assert res["project"]["target_format"] == "kicad10"
    assert res["project"]["format_upgrade"]["changed"] is True
    assert _stamp(tmp_path / "d10" / "d10.kicad_sch") == SCH_V10
    assert _stamp(tmp_path / "d10" / "d10.kicad_pcb") == PCB_V10


def test_create_project_kicad10_no_cli_refuses_before_writing(tmp_path: Path):
    fn = _create_project_fn(tmp_path)
    with patch("kicad_mcp.utils.platform_helper.find_kicad_cli", return_value=None):
        res = json.loads(fn(str(tmp_path / "d10x"), "d10x", target_format="kicad10"))
    assert res["error"] == "capability_unavailable"
    # No files written — the gate refused up front.
    assert not (tmp_path / "d10x" / "d10x.kicad_sch").exists()


def test_create_project_invalid_target_format(tmp_path: Path):
    fn = _create_project_fn(tmp_path)
    res = json.loads(fn(str(tmp_path / "bad"), "bad", target_format="kicad11"))
    assert res["status"] == "error"
    assert "target_format" in res["message"]
