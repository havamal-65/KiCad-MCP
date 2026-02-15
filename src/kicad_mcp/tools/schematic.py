"""Schematic tools - 7 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.validation import validate_kicad_path, validate_reference

logger = get_logger("tools.schematic")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
    """Register schematic tools on the MCP server."""

    @mcp.tool()
    def read_schematic(path: str) -> str:
        """Read a KiCad schematic and return its structure.

        Args:
            path: Path to .kicad_sch file.

        Returns:
            JSON with schematic info, symbols, wires, and labels.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        ops = backend.get_schematic_ops()
        result = ops.read_schematic(p)
        change_log.record("read_schematic", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_component(
        path: str,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
    ) -> str:
        """Add a component symbol to the schematic.

        Args:
            path: Path to .kicad_sch file.
            lib_id: Library symbol identifier (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').
            reference: Reference designator (e.g. R1, U1).
            value: Component value (e.g. '10k', '100nF').
            x: X position in schematic units (mm).
            y: Y position in schematic units (mm).

        Returns:
            JSON with placed component details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_component(p, lib_id, reference, value, x, y)
        change_log.record(
            "add_component",
            {"path": path, "lib_id": lib_id, "reference": reference, "value": value},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_wire(
        path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> str:
        """Add a wire connection between two points in the schematic.

        Args:
            path: Path to .kicad_sch file.
            start_x: Start X coordinate.
            start_y: Start Y coordinate.
            end_x: End X coordinate.
            end_y: End Y coordinate.

        Returns:
            JSON with wire endpoints and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_wire(p, start_x, start_y, end_x, end_y)
        change_log.record(
            "add_wire",
            {"path": path, "start": [start_x, start_y], "end": [end_x, end_y]},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_label(
        path: str,
        text: str,
        x: float,
        y: float,
        label_type: str = "net_label",
    ) -> str:
        """Add a net label to the schematic.

        Args:
            path: Path to .kicad_sch file.
            text: Label text (net name like VCC, GND, SDA).
            x: X position.
            y: Y position.
            label_type: Type of label - 'net_label', 'global_label', or 'hierarchical_label'.

        Returns:
            JSON with label details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        valid_types = {"net_label", "global_label", "hierarchical_label"}
        if label_type not in valid_types:
            return json.dumps({
                "status": "error",
                "message": f"Invalid label_type: {label_type}. Must be one of: {valid_types}",
            })

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_label(p, text, x, y, label_type)
        change_log.record(
            "add_label",
            {"path": path, "text": text, "x": x, "y": y, "label_type": label_type},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def annotate_schematic(path: str) -> str:
        """Auto-annotate component reference designators in the schematic.

        Args:
            path: Path to .kicad_sch file.

        Returns:
            JSON with annotation results.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        try:
            ops = backend.get_schematic_ops()
            result = ops.annotate(p)
            change_log.record(
                "annotate_schematic", {"path": path},
                file_modified=path,
                backup_path=str(backup) if backup else None,
            )
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            return json.dumps({
                "status": "info",
                "message": "Auto-annotation requires kicad-cli or KiCad IPC. "
                           "Not available with current backends.",
            })

    @mcp.tool()
    def generate_netlist(path: str, output: str) -> str:
        """Generate a netlist from the schematic.

        Args:
            path: Path to .kicad_sch file.
            output: Output path for the netlist file.

        Returns:
            JSON with netlist generation result.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        out = Path(output)

        try:
            ops = backend.get_schematic_ops()
            result = ops.generate_netlist(p, out)
            change_log.record("generate_netlist", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            return json.dumps({
                "status": "info",
                "message": "Netlist generation requires kicad-cli or KiCad IPC.",
            })
