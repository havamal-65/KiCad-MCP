"""PCB board tools - 8 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.validation import (
    validate_kicad_path,
    validate_layer,
    validate_net_name,
    validate_positive,
    validate_reference,
)

logger = get_logger("tools.board")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
    """Register PCB board tools on the MCP server."""

    @mcp.tool()
    def read_board(path: str) -> str:
        """Read a PCB board file and return its complete structure.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with board info, components, nets, and tracks.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        result = ops.read_board(p)
        change_log.record("read_board", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_board_info(path: str) -> str:
        """Get board metadata (title, revision, layers, component/net counts).

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with board metadata.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        info = ops.get_board_info(p)
        change_log.record("get_board_info", {"path": path})
        return json.dumps({"status": "success", "info": info}, indent=2)

    @mcp.tool()
    def place_component(
        path: str,
        reference: str,
        footprint: str,
        x: float,
        y: float,
        layer: str = "F.Cu",
        rotation: float = 0.0,
    ) -> str:
        """Place a component on the PCB board.

        Args:
            path: Path to .kicad_pcb file.
            reference: Component reference designator (e.g. U1, R1).
            footprint: Footprint library:name (e.g. 'Package_SO:SOIC-8').
            x: X position in mm.
            y: Y position in mm.
            layer: Layer name (default F.Cu).
            rotation: Rotation in degrees.

        Returns:
            JSON with placed component details.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_reference(reference)
        validate_layer(layer)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.place_component(p, reference, footprint, x, y, layer, rotation)
        change_log.record(
            "place_component",
            {"path": path, "reference": reference, "footprint": footprint, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def move_component(
        path: str,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> str:
        """Move an existing component to a new position.

        Args:
            path: Path to .kicad_pcb file.
            reference: Component reference (e.g. U1).
            x: New X position in mm.
            y: New Y position in mm.
            rotation: New rotation in degrees (optional, keeps current if not set).

        Returns:
            JSON with updated component position.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.move_component(p, reference, x, y, rotation)
        change_log.record(
            "move_component",
            {"path": path, "reference": reference, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_track(
        path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: float,
        layer: str = "F.Cu",
        net: str = "",
    ) -> str:
        """Add a copper track segment to the board.

        Args:
            path: Path to .kicad_pcb file.
            start_x: Start X coordinate in mm.
            start_y: Start Y coordinate in mm.
            end_x: End X coordinate in mm.
            end_y: End Y coordinate in mm.
            width: Track width in mm.
            layer: Layer name (default F.Cu).
            net: Net name to assign (optional).

        Returns:
            JSON with track details.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_positive(width, "width")
        validate_layer(layer)
        if net:
            validate_net_name(net)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.add_track(p, start_x, start_y, end_x, end_y, width, layer, net)
        change_log.record(
            "add_track",
            {"path": path, "start": [start_x, start_y], "end": [end_x, end_y], "width": width},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_via(
        path: str,
        x: float,
        y: float,
        size: float = 0.8,
        drill: float = 0.4,
        net: str = "",
        via_type: str = "through",
    ) -> str:
        """Add a via to the board.

        Args:
            path: Path to .kicad_pcb file.
            x: X position in mm.
            y: Y position in mm.
            size: Via outer diameter in mm (default 0.8).
            drill: Drill diameter in mm (default 0.4).
            net: Net name (optional).
            via_type: Via type - 'through', 'blind_buried', or 'micro'.

        Returns:
            JSON with via details.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_positive(size, "size")
        validate_positive(drill, "drill")
        if net:
            validate_net_name(net)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.add_via(p, x, y, size, drill, net, via_type)
        change_log.record(
            "add_via",
            {"path": path, "x": x, "y": y, "size": size, "drill": drill},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def assign_net(path: str, reference: str, pad: str, net: str) -> str:
        """Assign a net to a component pad.

        Args:
            path: Path to .kicad_pcb file.
            reference: Component reference (e.g. U1).
            pad: Pad number or name.
            net: Net name to assign.

        Returns:
            JSON with assignment result.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_reference(reference)
        validate_net_name(net)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.assign_net(p, reference, pad, net)
        change_log.record(
            "assign_net",
            {"path": path, "reference": reference, "pad": pad, "net": net},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_design_rules(path: str) -> str:
        """Get the board's design rules (clearances, track widths, via sizes).

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with design rule parameters.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        rules = ops.get_design_rules(p)
        change_log.record("get_design_rules", {"path": path})
        return json.dumps({"status": "success", "rules": rules}, indent=2)
