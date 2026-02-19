"""Design Rule Check tools - 4 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.drc")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
    """Register DRC/ERC tools on the MCP server."""

    @mcp.tool()
    def run_drc(path: str, output: str | None = None) -> str:
        """Run Design Rule Check on a PCB board.

        Checks for clearance violations, unconnected nets, track width violations,
        and other manufacturing constraint issues.

        Args:
            path: Path to .kicad_pcb file.
            output: Optional path for the DRC report file (JSON format).

        Returns:
            JSON with DRC results: passed/failed, error/warning counts, violations list.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else None

        try:
            drc_ops = backend.get_drc_ops()
            result = drc_ops.run_drc(p, out)
            change_log.record("run_drc", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"DRC failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def run_erc(path: str, output: str | None = None) -> str:
        """Run Electrical Rules Check on a schematic.

        Checks for unconnected pins, conflicting pin types, missing power flags,
        and other electrical connectivity issues.

        When kicad-cli is available, runs the full KiCad ERC. Otherwise falls
        back to file-based ERC lite which checks for duplicate references,
        floating pins, and missing power connections.

        Args:
            path: Path to .kicad_sch file.
            output: Optional path for the ERC report file (JSON format).

        Returns:
            JSON with ERC results: passed/failed, error/warning counts, violations list.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        out = Path(output) if output else None

        try:
            drc_ops = backend.get_drc_ops()
            result = drc_ops.run_erc(p, out)
            change_log.record("run_erc", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            # Fall back to file-based validation
            try:
                sch_ops = backend.get_schematic_ops()
                result = sch_ops.validate_schematic(p)
                result["backend"] = "file"
                result["note"] = "File-based ERC lite. For full ERC, install kicad-cli."
                change_log.record("run_erc", {"path": path, "output": output, "backend": "file"})
                return json.dumps({"status": "success", **result}, indent=2)
            except Exception as fallback_err:
                return json.dumps({
                    "status": "error",
                    "message": f"ERC failed: {fallback_err}. Neither kicad-cli nor file-based ERC available.",
                })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"ERC failed: {e}",
            })

    @mcp.tool()
    def validate_schematic(
        path: str,
        check_floating_pins: bool = True,
        check_duplicate_references: bool = True,
    ) -> str:
        """File-based electrical rules check (no kicad-cli needed).

        Performs basic ERC validation using file parsing and connectivity
        analysis. Checks for:
        - Duplicate reference designators (error)
        - Floating pins without no-connect markers (warning)
        - Unconnected power symbols (warning)

        This is a lightweight alternative to run_erc that works without
        kicad-cli installed.

        Args:
            path: Path to .kicad_sch file.
            check_floating_pins: Check for unconnected pins (default true).
            check_duplicate_references: Check for duplicate references (default true).

        Returns:
            JSON with {passed, violations, error_count, warning_count}.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        ops = backend.get_schematic_ops()
        try:
            result = ops.validate_schematic(p)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Schematic validation not supported by current backend.",
            })

        # Filter violations based on check flags
        if not check_floating_pins:
            result["violations"] = [
                v for v in result["violations"]
                if v["type"] != "floating_pin"
            ]
        if not check_duplicate_references:
            result["violations"] = [
                v for v in result["violations"]
                if v["type"] != "duplicate_reference"
            ]

        # Recount after filtering
        result["error_count"] = sum(
            1 for v in result["violations"] if v["severity"] == "error"
        )
        result["warning_count"] = sum(
            1 for v in result["violations"] if v["severity"] == "warning"
        )
        result["passed"] = result["error_count"] == 0

        change_log.record("validate_schematic", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_board_design_rules(path: str) -> str:
        """Get the design rules configured for a PCB board.

        Returns clearance constraints, track width limits, via size requirements,
        and other manufacturing rules.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with design rule parameters.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        rules = ops.get_design_rules(p)
        change_log.record("get_board_design_rules", {"path": path})
        return json.dumps({"status": "success", "rules": rules}, indent=2)
