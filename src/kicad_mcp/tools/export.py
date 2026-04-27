"""Export tools - 7 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.export")


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register export tools on the MCP server."""

    @mcp.tool()
    def export_gerbers(
        path: str,
        output_dir: str,
        layers: list[str] | None = None,
    ) -> str:
        """Export Gerber manufacturing files from a PCB board.

        Generates Gerber files for each copper and mask layer, plus drill files.
        These are the standard files needed for PCB fabrication.

        Args:
            path: Path to .kicad_pcb file.
            output_dir: Directory to write Gerber files to.
            layers: Optional list of specific layers to export. Exports all enabled layers if not specified.

        Returns:
            JSON with list of generated files and output directory.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_gerbers(p, out_dir, layers)
            change_log.record("export_gerbers", {"path": path, "output_dir": output_dir})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Gerber export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_drill(path: str, output_dir: str) -> str:
        """Export drill files (Excellon format) from a PCB board.

        Args:
            path: Path to .kicad_pcb file.
            output_dir: Directory to write drill files to.

        Returns:
            JSON with list of generated drill files.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_drill(p, out_dir)
            change_log.record("export_drill", {"path": path, "output_dir": output_dir})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Drill export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_bom(
        path: str,
        output: str,
        format: str = "csv",
    ) -> str:
        """Export Bill of Materials from a board or schematic.

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output file path.
            format: Output format - 'csv', 'json', 'xml', or 'html'.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path)
        out = Path(output)

        valid_formats = {"csv", "json", "xml", "html"}
        if format not in valid_formats:
            return json.dumps({
                "status": "error",
                "message": f"Invalid format: {format}. Must be one of: {valid_formats}",
            })

        try:
            if p.suffix == ".kicad_pcb":
                backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_bom(p, out, format)
            change_log.record("export_bom", {"path": path, "output": output, "format": format})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"BOM export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_pick_and_place(path: str, output: str) -> str:
        """Export pick-and-place (component placement) file for assembly.

        Args:
            path: Path to .kicad_pcb file.
            output: Output CSV file path.

        Returns:
            JSON with export result.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_pick_and_place(p, out)
            change_log.record("export_pick_and_place", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Pick-and-place export failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def export_pdf(
        path: str,
        output: str,
        layers: list[str] | None = None,
    ) -> str:
        """Export a board or schematic to PDF.

        Requires kicad-cli. If kicad-cli is not on PATH the error message will
        include the exact path that was searched so you can diagnose the problem.
        After export, verifies the output file exists — a missing file is an error
        even when kicad-cli returns exit code 0.

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output PDF file path.
            layers: For board files, optional list of layers to include.

        Returns:
            JSON with export result and output file path.
        """
        from kicad_mcp.utils.platform_helper import find_kicad_cli
        import shutil as _shutil

        p = validate_kicad_path(path)
        out = Path(output)

        # Verify kicad-cli is available before attempting export
        cli_path = _shutil.which("kicad-cli")
        if not cli_path:
            cli = find_kicad_cli()
            cli_path = str(cli) if cli else None
        if not cli_path:
            return json.dumps({
                "status": "error",
                "message": (
                    "export_pdf requires kicad-cli, which was not found on PATH or in "
                    "standard KiCad installation directories. "
                    "Install KiCad and ensure its bin/ directory is on PATH, or set "
                    "KICAD_MCP_CLI_PATH environment variable to the kicad-cli executable."
                ),
            }, indent=2)

        try:
            if p.suffix == ".kicad_pcb":
                backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_pdf(p, out, layers)

            # Surface failures: check exit code result and file existence
            if not result.get("success", True):
                cmd_hint = (
                    f"kicad-cli {'sch' if p.suffix == '.kicad_sch' else 'pcb'} export pdf "
                    f"--output {out} {p}"
                )
                return json.dumps({
                    "status": "error",
                    "message": f"PDF export failed. stderr: {result.get('message', 'no output')}",
                    "command_attempted": cmd_hint,
                    "kicad_cli_path": cli_path,
                }, indent=2)

            # Even a zero-exit kicad-cli sometimes produces no file (wrong path, permission issue)
            if not out.exists():
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"kicad-cli reported success but output file was not created: {out}. "
                        "Check that the output directory is writable and the path is correct."
                    ),
                    "output_path": str(out),
                }, indent=2)

            change_log.record("export_pdf", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": (
                    "PDF export is not implemented by the current backend. "
                    "Ensure kicad-cli is installed and on PATH."
                ),
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"PDF export failed: {e}",
                "kicad_cli_path": cli_path,
            }, indent=2)

    @mcp.tool()
    def export_step(path: str, output: str | None = None) -> str:
        """Export a 3D STEP model from a PCB board.

        Generates a STEP file suitable for mechanical integration and 3D viewer import.
        Requires kicad-cli (installed with KiCad).

        Args:
            path: Path to .kicad_pcb file.
            output: Output .step file path. Defaults to same directory as board with .step extension.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else p.with_suffix(".step")

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_step(p, out)
            change_log.record("export_step", {"path": path, "output": str(out)})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"STEP export failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def export_vrml(path: str, output: str | None = None) -> str:
        """Export a 3D VRML model from a PCB board.

        Generates a VRML file for 3D rendering and simulation tools.
        Requires kicad-cli (installed with KiCad).

        Args:
            path: Path to .kicad_pcb file.
            output: Output .wrl file path. Defaults to same directory as board with .wrl extension.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else p.with_suffix(".wrl")

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_vrml(p, out)
            change_log.record("export_vrml", {"path": path, "output": str(out)})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"VRML export failed: {e}. Requires kicad-cli backend.",
            })
