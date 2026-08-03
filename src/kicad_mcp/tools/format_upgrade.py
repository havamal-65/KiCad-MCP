"""KiCad project format upgrade (KIR3 / FB, F14).

Makes KiCad-10 project format reachable from the MCP. A project the MCP creates
is written in KiCad-9 format (the safe cross-version default), but the KiCad-10
variant tools require v10 format. Rather than hand-write v10 files (whose on-disk
grammar differs from v9 — tab indentation, extra tokens — so a v9 body with a v10
stamp would be a fake), this drives KiCad's own ``kicad-cli sch/pcb upgrade`` to
rewrite the files authoritatively.

One engine, ``upgrade_project_files``, serves both the ``upgrade_project_format``
tool and ``create_project(target_format="kicad10")``. The upgrade is one-way
(KiCad 9 cannot re-open a v10 file); the ``.kicad_mcp_backups`` v9 copies are the
only rollback, so every changed file is backed up first and restored on failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.variant_files import (
    PCB_MIN_FORMAT,
    SCH_MIN_FORMAT,
    ProjectFiles,
    VariantAuthorError,
    file_format_version,
    resolve_project,
)

logger = get_logger("tools.format_upgrade")

_UPGRADE_TIMEOUT = 120  # seconds; a format migration of one file is quick


def v10_cli_gate() -> tuple[Path | None, str | None]:
    """Resolve the serving kicad-cli and require it be KiCad 10+.

    Returns ``(cli, None)`` to proceed, or ``(None, error_json)`` — refuse before
    any file is touched. Reuses the ``variant-authoring`` probe (``--variant`` is a
    reliable v10 marker; ``sch upgrade --force`` exists on KiCad 9 too, so it would
    not guarantee a v10-format result).
    """
    from kicad_mcp.models.errors import KicadCapabilityError
    from kicad_mcp.utils.kicad_capabilities import requires_kicad
    from kicad_mcp.utils.platform_helper import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        return None, json.dumps({
            "status": "error",
            "error": "capability_unavailable",
            "capability": "format-upgrade",
            "required": "kicad-cli 10.0+",
            "detected": "kicad-cli not found",
            "hint": "Install KiCad 10 and ensure kicad-cli is discoverable.",
        }, indent=2)
    try:
        requires_kicad("variant-authoring", cli)
    except KicadCapabilityError as exc:
        return None, json.dumps({"status": "error", **exc.payload}, indent=2)
    return cli, None


def _pcbnew_note() -> str | None:
    from kicad_mcp.utils.platform_helper import is_pcbnew_running

    if is_pcbnew_running():
        return (
            "pcbnew appears to be running — the upgraded files were written to "
            "disk; reopen the project in KiCad 10 to see them."
        )
    return None


def _run_upgrade(cli: Path, subcmd: str, path: Path) -> int:
    """Run ``kicad-cli <subcmd> upgrade --force <path>`` (in place); return exit code."""
    try:
        result = subprocess.run(
            [str(cli), subcmd, "upgrade", "--force", str(path)],
            capture_output=True,
            text=True,
            timeout=_UPGRADE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("kicad-cli %s upgrade failed to run: %s", subcmd, exc)
        return -1
    if result.returncode != 0:
        logger.warning("kicad-cli %s upgrade rc=%s: %s", subcmd, result.returncode, result.stderr)
    return result.returncode


def upgrade_project_files(files: ProjectFiles, cli: Path) -> dict[str, Any]:
    """Upgrade a project's ``.kicad_sch`` / ``.kicad_pcb`` to KiCad-10 format.

    In place, via kicad-cli, backing up each changed file first and restoring all
    of them on any failure. The ``.kicad_pro`` is not CLI-upgradable (KiCad
    migrates project settings on open) and the variant gate never checks it.
    """
    targets: list[tuple[Path, str, int]] = []
    if files.sch is not None:
        targets.append((files.sch, "sch", SCH_MIN_FORMAT))
    if files.pcb is not None:
        targets.append((files.pcb, "pcb", PCB_MIN_FORMAT))

    stale: list[tuple[Path, str, int, int | None]] = []
    for path, subcmd, minimum in targets:
        stamp = file_format_version(path.read_text(encoding="utf-8"))
        if stamp is None or stamp < minimum:
            stale.append((path, subcmd, minimum, stamp))

    if not stale:
        return {
            "status": "success",
            "changed": False,
            "message": "already KiCad 10 format",
            "files": [],
        }

    reports: list[dict[str, Any]] = []
    backups: list[tuple[Path, Path]] = []
    for path, subcmd, minimum, before in stale:
        bkp = create_backup(path)
        if bkp is not None:
            backups.append((path, bkp))
        rc = _run_upgrade(cli, subcmd, path)
        after = file_format_version(path.read_text(encoding="utf-8"))
        if rc != 0 or after is None or after < minimum:
            for original, backup in backups:  # restore every changed file
                shutil.copy2(str(backup), str(original))
            return {
                "status": "error",
                "stage": f"{subcmd}_upgrade",
                "returncode": rc,
                "message": (
                    f"kicad-cli {subcmd} upgrade did not reach v10 format "
                    f"(found {after}, need {minimum}); project restored from backup."
                ),
                "files": reports,
            }
        reports.append({
            "file": str(path),
            "before": before,
            "after": after,
            "backup": str(bkp) if bkp is not None else None,
        })

    return {"status": "success", "changed": True, "files": reports}


def run_upgrade_project_format(project_path: str, change_log: ChangeLog | None = None) -> str:
    """Tool body: upgrade an existing project's files to KiCad-10 format."""
    try:
        files = resolve_project(Path(project_path))
    except VariantAuthorError as exc:
        return json.dumps({"status": "error", "error": "not_a_project", "message": str(exc)}, indent=2)

    cli, refusal = v10_cli_gate()
    if refusal is not None:
        return refusal
    assert cli is not None  # refusal is None => cli resolved

    result = upgrade_project_files(files, cli)
    note = _pcbnew_note()
    if note is not None:
        result["note"] = note
    if change_log is not None and result.get("status") == "success":
        change_log.record("upgrade_project_format", {"path": project_path, "changed": result.get("changed")})
    return json.dumps(result, indent=2)


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register the format-upgrade tool on the MCP server."""

    @mcp.tool()
    def upgrade_project_format(path: str) -> str:
        """Upgrade a KiCad project's files from KiCad-9 to KiCad-10 format.

        The KiCad-10 variant tools require v10-format files; a project created by
        the MCP is written in v9 format by default. This drives KiCad's own
        ``kicad-cli sch/pcb upgrade`` to rewrite the schematic and board in place,
        backing up every changed file first.

        Warning: the upgrade is one-way — KiCad 9 cannot re-open a v10 file. The v9
        copies under ``.kicad_mcp_backups/`` are the only rollback; they are never
        deleted. Refuses (without touching any file) if no KiCad-10 kicad-cli is
        available. The ``.kicad_pro`` is not upgraded (KiCad migrates it on open).

        Args:
            path: Path to any of the project's files (.kicad_pro/.kicad_sch/.kicad_pcb).

        Returns:
            JSON with changed flag and, per upgraded file, before/after format
            stamps and the backup path.
        """
        return run_upgrade_project_format(path, change_log)
