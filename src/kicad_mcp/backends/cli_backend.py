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


def _variant_flags(variant: str | list[str] | None) -> list[str]:
    """``--variant`` argv fragment (K10-S3). ``None`` → empty: the argv is
    byte-identical to pre-variant behavior (REQ-S3A-001, unit-guarded)."""
    if variant is None:
        return []
    names = [variant] if isinstance(variant, str) else list(variant)
    flags: list[str] = []
    for name in names:
        flags += ["--variant", name]
    return flags


def _expected_outputs(output: Path, variant: str | list[str] | None) -> list[Path]:
    """The artifact paths a call will produce: the literal *output* normally;
    when the path carries the ``${VARIANT}`` template, the per-variant
    expansion (for existence checks/reporting)."""
    if variant is None or "${VARIANT}" not in str(output):
        return [output]
    names = [variant] if isinstance(variant, str) else list(variant)
    return [Path(str(output).replace("${VARIANT}", v)) for v in names]


def _variant_calls(
    output: Path, variant: str | list[str] | None,
) -> list[tuple[list[str], Path]]:
    """(variant flags, concrete output path) — one CLI invocation per entry.

    A variant list is expanded CLIENT-side into one single-``--variant`` call
    per name: kicad-cli 10.0.4's own multi-``--variant``/``${VARIANT}`` loop
    writes default-variant content into every templated file (verified live
    2026-07-21 — upstream defect), while single-variant calls are correct.
    Per-variant calls stay byte-identical if/when upstream fixes the loop.
    """
    if isinstance(variant, list):
        return [
            (["--variant", name], Path(str(output).replace("${VARIANT}", name)))
            for name in variant
        ]
    return [(_variant_flags(variant), _expected_outputs(output, variant)[0])]


class CLIExportOps(ExportOps):
    """Export operations via kicad-cli subprocess."""

    def __init__(self, cli_path: Path) -> None:
        self._cli = cli_path

    def _run(self, args: list[str], timeout: int = CLI_TIMEOUT) -> subprocess.CompletedProcess[str]:
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
        *, variant: str | list[str] | None = None,
    ) -> dict[str, Any]:
        # Multi-variant: output_dir carries ${VARIANT}; one gerber call per
        # expanded dir (client-side loop — see _variant_calls), never the
        # literal template dir.
        results: list[subprocess.CompletedProcess[str]] = []
        out_dirs: list[Path] = []
        for flags, d in _variant_calls(output_dir, variant):
            d.mkdir(parents=True, exist_ok=True)
            out_dirs.append(d)
            args = [
                "pcb", "export", "gerbers",
                "--output", str(d) + "/",
                *flags,
                str(board_path),
            ]
            if layers:
                for layer in layers:
                    args.insert(-1, "--layers")
                    args.insert(-1, layer)
            results.append(self._run(args))

        # Also export drill files — drill has no variant surface (holes are
        # physical, identical across variants); one copy per output dir so
        # every variant's fab package is complete.
        for d in out_dirs:
            drill_args = [
                "pcb", "export", "drill",
                "--output", str(d) + "/",
                "--format", "excellon",
                "--drill-origin", "absolute",
                "--excellon-separate-th",
                str(board_path),
            ]
            self._run(drill_args)

        output_files = [
            str(f) for d in out_dirs if d.is_dir() for f in d.iterdir() if f.is_file()
        ]

        ok = all(r.returncode == 0 for r in results)
        first_err = next((r.stderr for r in results if r.returncode != 0), "")
        return {
            "success": ok,
            "output_dir": str(output_dir),
            "output_files": output_files,
            "message": first_err if not ok else "Gerbers exported",
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
        *, variant: str | list[str] | None = None, exclude_dnp: bool = False,
    ) -> dict[str, Any]:
        # sch vs pcb subcommand — for PCB, use python-bom or extract from schematic
        sub = "sch" if path.suffix == ".kicad_sch" else "pcb"

        results: list[subprocess.CompletedProcess[str]] = []
        for flags, out in _variant_calls(output, variant):
            out.parent.mkdir(parents=True, exist_ok=True)
            args = [
                sub, "export", "bom",
                "--output", str(out),
                *flags,
                *(["--exclude-dnp"] if exclude_dnp else []),
                str(path),
            ]
            results.append(self._run(args))

        ok = all(r.returncode == 0 for r in results)
        first_err = next((r.stderr for r in results if r.returncode != 0), "")
        expected = _expected_outputs(output, variant)
        return {
            "success": ok,
            "output_files": [str(p) for p in expected] if ok else [],
            "message": first_err if not ok else "BOM exported",
        }

    def export_pick_and_place(
        self, board_path: Path, output: Path,
        *, variant: str | list[str] | None = None, exclude_dnp: bool = False,
    ) -> dict[str, Any]:
        results: list[subprocess.CompletedProcess[str]] = []
        for flags, out in _variant_calls(output, variant):
            out.parent.mkdir(parents=True, exist_ok=True)
            args = [
                "pcb", "export", "pos",
                "--output", str(out),
                "--format", "csv",
                "--units", "mm",
                *flags,
                *(["--exclude-dnp"] if exclude_dnp else []),
                str(board_path),
            ]
            results.append(self._run(args))

        ok = all(r.returncode == 0 for r in results)
        first_err = next((r.stderr for r in results if r.returncode != 0), "")
        expected = _expected_outputs(output, variant)
        return {
            "success": ok,
            "output_files": [str(p) for p in expected] if ok else [],
            "message": first_err if not ok else "Pick-and-place exported",
        }

    def export_pdf(
        self, path: Path, output: Path, layers: list[str] | None = None,
        *, variant: str | list[str] | None = None,
    ) -> dict[str, Any]:
        if path.suffix != ".kicad_sch" and variant is not None:
            # kicad-cli pcb export pdf has no --variant surface; the tool layer
            # refuses board+variant before reaching here (REQ-S3A-001).
            raise ExportError(
                "PDF variant export is schematic-only: "
                "kicad-cli pcb export pdf has no --variant flag"
            )

        results: list[subprocess.CompletedProcess[str]] = []
        for flags, out in _variant_calls(output, variant):
            out.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".kicad_sch":
                args = [
                    "sch", "export", "pdf",
                    "--output", str(out),
                    *flags,
                    str(path),
                ]
            else:
                args = [
                    "pcb", "export", "pdf",
                    "--output", str(out),
                    str(path),
                ]
                if layers:
                    args.insert(-1, "--layers")
                    args.insert(-1, ",".join(layers))
            results.append(self._run(args))

        ok = all(r.returncode == 0 for r in results)
        first_err = next((r.stderr for r in results if r.returncode != 0), "")
        expected = _expected_outputs(output, variant)
        return {
            "success": ok,
            "output_files": [str(p) for p in expected] if ok else [],
            "message": first_err if not ok else "PDF exported",
        }


    def export_step(self, board_path: Path, output_path: Path) -> dict[str, Any]:
        """Export 3D STEP model via kicad-cli pcb export step."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            ["pcb", "export", "step", "--output", str(output_path), str(board_path)],
            timeout=EXPORT_3D_TIMEOUT,
        )
        return {
            "success": result.returncode == 0,
            "output_file": str(output_path) if result.returncode == 0 else None,
            "message": result.stderr if result.returncode != 0 else "STEP exported",
        }

    def export_vrml(self, board_path: Path, output_path: Path) -> dict[str, Any]:
        """Export 3D VRML model via kicad-cli pcb export vrml."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            ["pcb", "export", "vrml", "--output", str(output_path), str(board_path)],
            timeout=EXPORT_3D_TIMEOUT,
        )
        return {
            "success": result.returncode == 0,
            "output_file": str(output_path) if result.returncode == 0 else None,
            "message": result.stderr if result.returncode != 0 else "VRML exported",
        }


class CLIDRCOps(DRCOps):
    """DRC/ERC operations via kicad-cli."""

    def __init__(self, cli_path: Path) -> None:
        self._cli = cli_path

    def _run(self, args: list[str], timeout: int = CLI_TIMEOUT) -> subprocess.CompletedProcess[str]:
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

        # kicad-cli pcb drc re-serialises the .kicad_pro on open, overwriting
        # any design-rule changes made since the project was last saved by KiCad.
        # Preserve the file and restore it after the DRC subprocess exits.
        pro_path = board_path.with_suffix(".kicad_pro")
        pro_backup: bytes | None = None
        if pro_path.exists():
            pro_backup = pro_path.read_bytes()

        try:
            result = self._run(args)
        finally:
            if pro_backup is not None:
                pro_path.write_bytes(pro_backup)



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
