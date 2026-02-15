"""MCP resource definitions - 6 resources."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger

logger = get_logger("resources")


def register_resources(mcp: FastMCP, backend: CompositeBackend) -> None:
    """Register MCP resources on the server."""

    @mcp.resource("kicad://project/{path}")
    def project_resource(path: str) -> str:
        """Get KiCad project information.

        Returns project structure including board, schematic, and library files.
        """
        from kicad_mcp.utils.kicad_paths import resolve_project_files
        p = Path(path).resolve()
        files = resolve_project_files(p)

        result = {
            "name": p.stem if p.is_file() else p.name,
            "path": str(p),
            "board_file": str(files["board"]) if files["board"] else None,
            "schematic_file": str(files["schematic"]) if files["schematic"] else None,
        }

        # Read project metadata if available
        pro_file = files.get("project")
        if pro_file and pro_file.exists():
            try:
                pro_data = json.loads(pro_file.read_text(encoding="utf-8"))
                result["metadata"] = pro_data.get("meta", {})
            except (json.JSONDecodeError, OSError):
                pass

        return json.dumps(result, indent=2)

    @mcp.resource("kicad://board/{path}/summary")
    def board_summary_resource(path: str) -> str:
        """Get a summary of a PCB board.

        Returns component count, net count, layer info, and board dimensions.
        """
        from kicad_mcp.utils.validation import validate_kicad_path
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        info = ops.get_board_info(p)
        return json.dumps(info, indent=2)

    @mcp.resource("kicad://board/{path}/components")
    def board_components_resource(path: str) -> str:
        """Get all components on a PCB board.

        Returns reference designators, values, footprints, and positions.
        """
        from kicad_mcp.utils.validation import validate_kicad_path
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        components = ops.get_components(p)
        return json.dumps({"components": components, "count": len(components)}, indent=2)

    @mcp.resource("kicad://board/{path}/nets")
    def board_nets_resource(path: str) -> str:
        """Get all nets on a PCB board.

        Returns net names and numbers.
        """
        from kicad_mcp.utils.validation import validate_kicad_path
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        nets = ops.get_nets(p)
        return json.dumps({"nets": nets, "count": len(nets)}, indent=2)

    @mcp.resource("kicad://board/{path}/rules")
    def board_rules_resource(path: str) -> str:
        """Get design rules for a PCB board.

        Returns clearance, track width, and via size constraints.
        """
        from kicad_mcp.utils.validation import validate_kicad_path
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        rules = ops.get_design_rules(p)
        return json.dumps(rules, indent=2)

    @mcp.resource("kicad://backends")
    def backends_resource() -> str:
        """Get information about available KiCad backends.

        Returns which backends are active, their versions, and capability routing.
        """
        status = backend.get_status()
        return json.dumps(status, indent=2)
