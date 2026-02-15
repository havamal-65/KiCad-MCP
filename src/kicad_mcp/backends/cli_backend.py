"""kicad-cli subprocess backend for headless operations."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    DRCOps,
    ExportOps,
    KiCadBackend,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import ExportError
from kicad_mcp.utils.platform_helper import find_kicad_cli

logger = get_logger("backend.cli")

CLI_TIMEOUT = 120  # seconds
EXPORT_3D_TIMEOUT = 300  # 5 minutes for 3D exports


class CLIExportOps(ExportOps):
    """Export operations via kicad-cli subprocess."""

    def __init__(self, cli_path: Path) -> None:
        self._cli = cli_path

    def _run(self, args: list[str], timeout: int = CLI_TIMEOUT) -> subprocess.CompletedProcess:
        cmd = [str(self._cli)] + args
        logger.debug("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.error("kicad-cli error: %s", result.stderr)
            return result
        except subprocess.TimeoutExpired:
            raise ExportError(f"kicad-cli timed out after {timeout}s")
        except OSError as e:
            raise ExportError(f"Failed to run kicad-cli: {e}")

    def export_gerbers(
        self, board_path: Path, output_dir: Path, layers: list[str] | None = None,
    ) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb", "export", "gerbers",
            "--output", str(output_dir) + "/",
            str(board_path),
        ]
        if layers:
            for layer in layers:
                args.insert(-1, "--layers")
                args.insert(-1, layer)

        result = self._run(args)

        # Also export drill files
        drill_args = [
            "pcb", "export", "drill",
            "--output", str(output_dir) + "/",
            "--format", "excellon",
            "--drill-origin", "absolute",
            "--excellon-separate-th",
            str(board_path),
        ]
        self._run(drill_args)

        output_files = [str(f) for f in output_dir.iterdir() if f.is_file()]

        return {
            "success": result.returncode == 0,
            "output_dir": str(output_dir),
            "output_files": output_files,
            "message": result.stderr if result.returncode != 0 else "Gerbers exported",
        }

    def export_drill(self, board_path: Path, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb", "export", "drill",
            "--output", str(output_dir) + "/",
            "--format", "excellon",
            "--drill-origin", "absolute",
            "--excellon-separate-th",
            str(board_path),
        ]
        result = self._run(args)
        output_files = [str(f) for f in output_dir.iterdir() if f.is_file()]

        return {
            "success": result.returncode == 0,
            "output_dir": str(output_dir),
            "output_files": output_files,
            "message": result.stderr if result.returncode != 0 else "Drill files exported",
        }

    def export_bom(
        self, path: Path, output: Path, fmt: str = "csv",
    ) -> dict[str, Any]:
        output.parent.mkdir(parents=True, exist_ok=True)

        # Determine if this is a schematic or board
        if path.suffix == ".kicad_sch":
            args = [
                "sch", "export", "bom",
                "--output", str(output),
                str(path),
            ]
        else:
            # For PCB, use python-bom or extract from schematic
            args = [
                "pcb", "export", "bom",
                "--output", str(output),
                str(path),
            ]

        result = self._run(args)
        return {
            "success": result.returncode == 0,
            "output_files": [str(output)] if result.returncode == 0 else [],
            "message": result.stderr if result.returncode != 0 else "BOM exported",
        }

    def export_pick_and_place(
        self, board_path: Path, output: Path,
    ) -> dict[str, Any]:
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb", "export", "pos",
            "--output", str(output),
            "--format", "csv",
            "--units", "mm",
            str(board_path),
        ]
        result = self._run(args)
        return {
            "success": result.returncode == 0,
            "output_files": [str(output)] if result.returncode == 0 else [],
            "message": result.stderr if result.returncode != 0 else "Pick-and-place exported",
        }

    def export_pdf(
        self, path: Path, output: Path, layers: list[str] | None = None,
    ) -> dict[str, Any]:
        output.parent.mkdir(parents=True, exist_ok=True)

        if path.suffix == ".kicad_sch":
            args = [
                "sch", "export", "pdf",
                "--output", str(output),
                str(path),
            ]
        else:
            args = [
                "pcb", "export", "pdf",
                "--output", str(output),
                str(path),
            ]
            if layers:
                for layer in layers:
                    args.insert(-1, "--layers")
                    args.insert(-1, layer)

        result = self._run(args)
        return {
            "success": result.returncode == 0,
            "output_files": [str(output)] if result.returncode == 0 else [],
            "message": result.stderr if result.returncode != 0 else "PDF exported",
        }


class CLIDRCOps(DRCOps):
    """DRC/ERC operations via kicad-cli."""

    def __init__(self, cli_path: Path) -> None:
        self._cli = cli_path

    def _run(self, args: list[str], timeout: int = CLI_TIMEOUT) -> subprocess.CompletedProcess:
        cmd = [str(self._cli)] + args
        logger.debug("Running: %s", " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ExportError(f"kicad-cli timed out after {timeout}s")
        except OSError as e:
            raise ExportError(f"Failed to run kicad-cli: {e}")

    def run_drc(self, board_path: Path, output: Path | None = None) -> dict[str, Any]:
        if output is None:
            output = Path(tempfile.mktemp(suffix=".json"))

        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb", "drc",
            "--output", str(output),
            "--format", "json",
            "--severity-all",
            str(board_path),
        ]
        result = self._run(args)

        drc_result: dict[str, Any] = {
            "passed": result.returncode == 0,
            "report_file": str(output),
            "violations": [],
            "error_count": 0,
            "warning_count": 0,
        }

        if output.exists():
            try:
                report = json.loads(output.read_text(encoding="utf-8"))
                violations = report.get("violations", [])
                for v in violations:
                    severity = v.get("severity", "error")
                    if severity == "error":
                        drc_result["error_count"] += 1
                    elif severity == "warning":
                        drc_result["warning_count"] += 1
                    drc_result["violations"].append({
                        "severity": severity,
                        "type": v.get("type", ""),
                        "description": v.get("description", ""),
                        "items": v.get("items", []),
                    })
                drc_result["passed"] = drc_result["error_count"] == 0
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to parse DRC report: %s", e)

        return drc_result

    def run_erc(self, schematic_path: Path, output: Path | None = None) -> dict[str, Any]:
        if output is None:
            output = Path(tempfile.mktemp(suffix=".json"))

        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "sch", "erc",
            "--output", str(output),
            "--format", "json",
            "--severity-all",
            str(schematic_path),
        ]
        result = self._run(args)

        erc_result: dict[str, Any] = {
            "passed": result.returncode == 0,
            "report_file": str(output),
            "violations": [],
            "error_count": 0,
            "warning_count": 0,
        }

        if output.exists():
            try:
                report = json.loads(output.read_text(encoding="utf-8"))
                for sheet in report.get("sheets", []):
                    for v in sheet.get("violations", []):
                        severity = v.get("severity", "error")
                        if severity == "error":
                            erc_result["error_count"] += 1
                        elif severity == "warning":
                            erc_result["warning_count"] += 1
                        erc_result["violations"].append({
                            "severity": severity,
                            "type": v.get("type", ""),
                            "description": v.get("description", ""),
                            "items": v.get("items", []),
                        })
                erc_result["passed"] = erc_result["error_count"] == 0
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to parse ERC report: %s", e)

        return erc_result


class CLIBackend(KiCadBackend):
    """Backend using kicad-cli for headless operations."""

    def __init__(self, cli_path: Path | None = None) -> None:
        self._cli_path = cli_path or find_kicad_cli()

    @property
    def name(self) -> str:
        return "cli"

    @property
    def capabilities(self) -> set[BackendCapability]:
        return {
            BackendCapability.DRC,
            BackendCapability.ERC,
            BackendCapability.EXPORT_GERBER,
            BackendCapability.EXPORT_DRILL,
            BackendCapability.EXPORT_PDF,
            BackendCapability.EXPORT_BOM,
            BackendCapability.EXPORT_PICK_AND_PLACE,
        }

    def is_available(self) -> bool:
        return self._cli_path is not None and self._cli_path.exists()

    def get_version(self) -> str | None:
        if not self._cli_path:
            return None
        try:
            result = subprocess.run(
                [str(self._cli_path), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def get_export_ops(self) -> CLIExportOps | None:
        if self._cli_path:
            return CLIExportOps(self._cli_path)
        return None

    def get_drc_ops(self) -> CLIDRCOps | None:
        if self._cli_path:
            return CLIDRCOps(self._cli_path)
        return None
