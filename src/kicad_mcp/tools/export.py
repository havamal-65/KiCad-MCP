"""Export tools - 5 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.export")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
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

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output PDF file path.
            layers: For board files, optional list of layers to include.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path)
        out = Path(output)

        try:
            ops = backend.get_export_ops()
            result = ops.export_pdf(p, out, layers)
            change_log.record("export_pdf", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"PDF export failed: {e}. Requires kicad-cli or SWIG backend.",
            })
