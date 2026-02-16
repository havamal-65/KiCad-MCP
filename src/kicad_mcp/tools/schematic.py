"""Schematic tools - 16 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.validation import validate_kicad_path, validate_reference, validate_writable_path

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
    def create_schematic(
        path: str,
        title: str = "",
        revision: str = "",
    ) -> str:
        """Create a new, empty KiCad schematic file.

        Generates a minimal valid KiCad 8+ schematic with proper structure
        including version, generator, UUID, paper size, lib_symbols, and
        sheet_instances. Components can then be added with add_component.

        Args:
            path: Path for the new .kicad_sch file. Parent directory must exist.
            title: Optional schematic title for the title block.
            revision: Optional revision string for the title block.

        Returns:
            JSON with created file path and UUID.
        """
        p = validate_writable_path(path, ".kicad_sch")
        if p.exists():
            return json.dumps({
                "status": "error",
                "message": f"File already exists: {p}. Use read_schematic to work with existing files.",
            })
        ops = backend.get_schematic_ops()
        try:
            result = ops.create_schematic(p, title=title, revision=revision)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Schematic creation not supported by current backend.",
            })
        change_log.record(
            "create_schematic",
            {"path": path, "title": title, "revision": revision},
            file_modified=path,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_component(
        path: str,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        mirror: str | None = None,
        footprint: str = "",
        properties: dict[str, str] | None = None,
    ) -> str:
        """Add a component symbol to the schematic.

        Args:
            path: Path to .kicad_sch file.
            lib_id: Library symbol identifier (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').
            reference: Reference designator (e.g. R1, U1).
            value: Component value (e.g. '10k', '100nF').
            x: X position in schematic units (mm).
            y: Y position in schematic units (mm).
            rotation: Rotation in degrees (0, 90, 180, 270).
            mirror: Mirror axis - 'x' or 'y', or None for no mirror.
            footprint: Footprint lib_id (e.g. 'Resistor_SMD:R_0402_1005Metric').
            properties: Additional properties dict (e.g. {'Datasheet': '...', 'MPN': '...'}).

        Returns:
            JSON with placed component details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_component(
            p, lib_id, reference, value, x, y,
            rotation=rotation, mirror=mirror,
            footprint=footprint, properties=properties,
        )
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

    @mcp.tool()
    def get_symbol_pin_positions(path: str, reference: str) -> str:
        """Get absolute schematic coordinates for each pin of a placed symbol.

        This is essential for knowing where to connect wires. It reads the symbol's
        placement (position, rotation, mirror) and its library pin definitions,
        then transforms each pin into absolute schematic coordinates.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the symbol (e.g. 'U1', 'R3').

        Returns:
            JSON with pin_positions mapping pin numbers to {x, y} coordinates.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        ops = backend.get_schematic_ops()
        result = ops.get_symbol_pin_positions(p, reference)
        change_log.record(
            "get_symbol_pin_positions",
            {"path": path, "reference": reference},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_no_connect(path: str, x: float, y: float) -> str:
        """Add a no-connect (X) marker to the schematic at the given position.

        No-connect markers indicate that a pin is intentionally left unconnected.
        Place them exactly on the pin endpoint.

        Args:
            path: Path to .kicad_sch file.
            x: X position (should match pin endpoint).
            y: Y position (should match pin endpoint).

        Returns:
            JSON with no-connect position and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_no_connect(p, x, y)
        change_log.record(
            "add_no_connect",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_power_symbol(
        path: str,
        name: str,
        x: float,
        y: float,
        rotation: float = 0.0,
    ) -> str:
        """Add a power symbol (e.g. +3V3, GND, +5V) to the schematic.

        Power symbols represent power nets. Common names: +3V3, +5V, GND, VCC, VDD.
        The lib_id is automatically set to 'power:<name>'.

        Args:
            path: Path to .kicad_sch file.
            name: Power net name (e.g. '+3V3', 'GND', '+5V', 'VCC').
            x: X position.
            y: Y position.
            rotation: Rotation in degrees (e.g. 180 for GND symbols pointing down).

        Returns:
            JSON with power symbol details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_power_symbol(p, name, x, y, rotation)
        change_log.record(
            "add_power_symbol",
            {"path": path, "name": name, "x": x, "y": y, "rotation": rotation},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def remove_component(path: str, reference: str) -> str:
        """Remove a component symbol from the schematic by reference designator.

        Completely removes the symbol instance block (e.g. the placed R1 resistor)
        from the schematic file. This does NOT remove associated wires or labels.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component to remove (e.g. 'R1', 'U3').

        Returns:
            JSON confirming removal.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        try:
            result = ops.remove_component(p, reference)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_component",
            {"path": path, "reference": reference},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def remove_wire(
        path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> str:
        """Remove a wire segment from the schematic identified by its endpoints.

        The wire is matched by comparing start/end coordinates within a small
        tolerance (0.01 mm). Use read_schematic to find exact wire coordinates.

        Args:
            path: Path to .kicad_sch file.
            start_x: Start X coordinate of the wire.
            start_y: Start Y coordinate of the wire.
            end_x: End X coordinate of the wire.
            end_y: End Y coordinate of the wire.

        Returns:
            JSON confirming removal.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        try:
            result = ops.remove_wire(p, start_x, start_y, end_x, end_y)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_wire",
            {"path": path, "start": [start_x, start_y], "end": [end_x, end_y]},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def remove_no_connect(path: str, x: float, y: float) -> str:
        """Remove a no-connect (X) marker from the schematic at the given position.

        The no-connect is matched by comparing its position within a small
        tolerance (0.01 mm). Use read_schematic to find exact no-connect positions.

        Args:
            path: Path to .kicad_sch file.
            x: X position of the no-connect marker.
            y: Y position of the no-connect marker.

        Returns:
            JSON confirming removal.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        try:
            result = ops.remove_no_connect(p, x, y)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_no_connect",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def move_schematic_component(
        path: str,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> str:
        """Move a schematic component to a new position.

        Updates the symbol's placement coordinates and shifts all property
        label positions by the same delta so they stay aligned. Optionally
        updates the rotation.

        Note: This is for schematic symbols. Use move_component for PCB footprints.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component to move (e.g. 'R1', 'U3').
            x: New X position in schematic units (mm).
            y: New Y position in schematic units (mm).
            rotation: New rotation in degrees (0, 90, 180, 270). Leave None to keep current.

        Returns:
            JSON with new position and rotation.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        try:
            result = ops.move_component(p, reference, x, y, rotation=rotation)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "move_schematic_component",
            {"path": path, "reference": reference, "x": x, "y": y, "rotation": rotation},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def update_component_property(
        path: str,
        reference: str,
        property_name: str,
        property_value: str,
    ) -> str:
        """Update or add a property on a placed schematic component.

        Can modify Value, Footprint, Datasheet, MPN, or any custom property.
        If the property does not exist on the component, it will be added (hidden by default).

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component (e.g. 'R1', 'U3').
            property_name: Property name (e.g. 'Value', 'Footprint', 'Datasheet', 'MPN').
            property_value: New value for the property.

        Returns:
            JSON confirming the update.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        try:
            result = ops.update_component_property(p, reference, property_name, property_value)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "update_component_property",
            {"path": path, "reference": reference,
             "property": property_name, "value": property_value},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def compare_schematic_pcb(schematic_path: str, board_path: str) -> str:
        """Compare schematic and PCB to find mismatches.

        Read-only diagnostic that detects reference designator mismatches,
        missing components, footprint differences, and value differences
        between a schematic and its associated PCB.

        Power symbols (references starting with '#') are excluded from
        the comparison since they don't appear in the PCB.

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.

        Returns:
            JSON with comparison results: missing_from_pcb, missing_from_schematic,
            footprint_mismatches, value_mismatches, and matched count.
        """
        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")

        sch_ops = backend.get_schematic_ops()
        pcb_ops = backend.get_board_ops()

        sch_data = sch_ops.read_schematic(sch_p)
        pcb_data = pcb_ops.read_board(pcb_p)

        # Build dicts keyed by reference, filtering out power symbols
        sch_by_ref: dict[str, dict] = {}
        for sym in sch_data.get("symbols", []):
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#"):
                continue
            if sym.get("is_power"):
                continue
            sch_by_ref[ref] = sym

        pcb_by_ref: dict[str, dict] = {}
        for comp in pcb_data.get("components", []):
            ref = comp.get("reference", "")
            if ref:
                pcb_by_ref[ref] = comp

        all_refs = set(sch_by_ref) | set(pcb_by_ref)
        missing_from_pcb = []
        missing_from_schematic = []
        footprint_mismatches = []
        value_mismatches = []
        matched = 0

        for ref in sorted(all_refs):
            in_sch = ref in sch_by_ref
            in_pcb = ref in pcb_by_ref

            if in_sch and not in_pcb:
                missing_from_pcb.append({
                    "reference": ref,
                    "value": sch_by_ref[ref].get("value", ""),
                    "lib_id": sch_by_ref[ref].get("lib_id", ""),
                })
                continue
            if in_pcb and not in_sch:
                missing_from_schematic.append({
                    "reference": ref,
                    "value": pcb_by_ref[ref].get("value", ""),
                    "footprint": pcb_by_ref[ref].get("footprint", ""),
                })
                continue

            # Both exist — compare
            sch_sym = sch_by_ref[ref]
            pcb_comp = pcb_by_ref[ref]
            mismatch = False

            sch_fp = sch_sym.get("footprint", "")
            pcb_fp = pcb_comp.get("footprint", "")
            if sch_fp and pcb_fp and sch_fp != pcb_fp:
                footprint_mismatches.append({
                    "reference": ref,
                    "schematic_footprint": sch_fp,
                    "pcb_footprint": pcb_fp,
                })
                mismatch = True

            sch_val = sch_sym.get("value", "")
            pcb_val = pcb_comp.get("value", "")
            if sch_val and pcb_val and sch_val != pcb_val:
                value_mismatches.append({
                    "reference": ref,
                    "schematic_value": sch_val,
                    "pcb_value": pcb_val,
                })
                mismatch = True

            if not mismatch:
                matched += 1

        result = {
            "schematic": str(sch_p),
            "board": str(pcb_p),
            "summary": {
                "schematic_components": len(sch_by_ref),
                "pcb_components": len(pcb_by_ref),
                "matched": matched,
                "missing_from_pcb": len(missing_from_pcb),
                "missing_from_schematic": len(missing_from_schematic),
                "footprint_mismatches": len(footprint_mismatches),
                "value_mismatches": len(value_mismatches),
            },
            "missing_from_pcb": missing_from_pcb,
            "missing_from_schematic": missing_from_schematic,
            "footprint_mismatches": footprint_mismatches,
            "value_mismatches": value_mismatches,
        }

        change_log.record(
            "compare_schematic_pcb",
            {"schematic_path": schematic_path, "board_path": board_path},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_pin_net(path: str, reference: str, pin_number: str) -> str:
        """Get the net name connected to a specific pin of a placed symbol.

        Uses schematic connectivity analysis (wires, labels, power symbols)
        to determine which net a pin belongs to.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the symbol (e.g. 'R1', 'U1').
            pin_number: Pin number as defined in the symbol library (e.g. '1', '2').

        Returns:
            JSON with reference, pin_number, net_name, and position.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        ops = backend.get_schematic_ops()
        try:
            result = ops.get_pin_net(p, reference, pin_number)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Net connectivity queries not supported by current backend.",
            })
        change_log.record(
            "get_pin_net",
            {"path": path, "reference": reference, "pin_number": pin_number},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_net_connections(path: str, net_name: str) -> str:
        """Get all connections on a named net in the schematic.

        Returns every pin, label, and wire segment belonging to the given net,
        determined by schematic connectivity analysis.

        Args:
            path: Path to .kicad_sch file.
            net_name: The net name to query (e.g. 'VCC', 'GND', 'Net-(R1-1)').

        Returns:
            JSON with net_name, pins list, labels list, and wires list.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        ops = backend.get_schematic_ops()
        try:
            result = ops.get_net_connections(p, net_name)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Net connectivity queries not supported by current backend.",
            })
        change_log.record(
            "get_net_connections",
            {"path": path, "net_name": net_name},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def sync_schematic_to_pcb(schematic_path: str, board_path: str) -> str:
        """Synchronize schematic components to the PCB board.

        Reads the schematic and PCB, compares them, and applies safe
        automatic changes:
        - Components missing from PCB are placed at auto-positioned locations.
        - Value mismatches are updated on the PCB side.
        - Footprint mismatches and extra PCB components are reported as warnings
          (not auto-fixed, as they require manual review).

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.

        Returns:
            JSON with summary of actions taken and warnings.
        """
        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")

        sch_ops = backend.get_schematic_ops()
        pcb_ops = backend.get_board_ops()

        sch_data = sch_ops.read_schematic(sch_p)
        pcb_data = pcb_ops.read_board(pcb_p)

        # Build dicts keyed by reference, filtering out power symbols
        sch_by_ref: dict[str, dict] = {}
        for sym in sch_data.get("symbols", []):
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#"):
                continue
            if sym.get("is_power"):
                continue
            sch_by_ref[ref] = sym

        pcb_by_ref: dict[str, dict] = {}
        for comp in pcb_data.get("components", []):
            ref = comp.get("reference", "")
            if ref:
                pcb_by_ref[ref] = comp

        actions: list[dict] = []
        warnings: list[dict] = []

        # Try to get board_modify_ops for placing components
        try:
            board_modify_ops = backend.get_board_modify_ops()
        except Exception:
            board_modify_ops = None

        # Auto-position grid for new components
        place_x, place_y = 50.0, 50.0
        place_step = 10.0

        for ref in sorted(sch_by_ref):
            if ref not in pcb_by_ref:
                # Missing from PCB → place it
                sym = sch_by_ref[ref]
                footprint = sym.get("footprint", "")
                if not footprint:
                    warnings.append({
                        "type": "no_footprint",
                        "reference": ref,
                        "message": f"{ref} has no footprint assigned in schematic, cannot place on PCB.",
                    })
                    continue

                if board_modify_ops is not None:
                    backup = create_backup(pcb_p)
                    try:
                        result = board_modify_ops.place_component(
                            pcb_p, ref, footprint, place_x, place_y,
                        )
                        actions.append({
                            "type": "placed",
                            "reference": ref,
                            "footprint": footprint,
                            "position": {"x": place_x, "y": place_y},
                        })
                        place_x += place_step
                        if place_x > 200.0:
                            place_x = 50.0
                            place_y += place_step
                    except Exception as exc:
                        warnings.append({
                            "type": "place_failed",
                            "reference": ref,
                            "message": str(exc),
                        })
                else:
                    warnings.append({
                        "type": "missing_from_pcb",
                        "reference": ref,
                        "footprint": footprint,
                        "message": f"{ref} missing from PCB (board modify not available).",
                    })

        for ref in sorted(pcb_by_ref):
            if ref not in sch_by_ref:
                warnings.append({
                    "type": "extra_in_pcb",
                    "reference": ref,
                    "message": f"{ref} exists in PCB but not in schematic.",
                })

        # Check mismatches for components in both
        for ref in sorted(set(sch_by_ref) & set(pcb_by_ref)):
            sch_sym = sch_by_ref[ref]
            pcb_comp = pcb_by_ref[ref]

            sch_fp = sch_sym.get("footprint", "")
            pcb_fp = pcb_comp.get("footprint", "")
            if sch_fp and pcb_fp and sch_fp != pcb_fp:
                warnings.append({
                    "type": "footprint_mismatch",
                    "reference": ref,
                    "schematic_footprint": sch_fp,
                    "pcb_footprint": pcb_fp,
                    "message": f"{ref} footprint mismatch: schematic={sch_fp}, pcb={pcb_fp}",
                })

            sch_val = sch_sym.get("value", "")
            pcb_val = pcb_comp.get("value", "")
            if sch_val and pcb_val and sch_val != pcb_val:
                # Update PCB value
                if board_modify_ops is not None:
                    try:
                        # Update the Value property via text replacement
                        content = pcb_p.read_text(encoding="utf-8")
                        from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference
                        location = find_footprint_block_by_reference(content, ref)
                        if location:
                            start, end = location
                            block = content[start:end + 1]
                            # Replace the Value property
                            import re
                            val_pattern = re.compile(
                                r'(\(property\s+"Value"\s+)"([^"]*)"'
                            )
                            match = val_pattern.search(block)
                            if match:
                                new_block = (
                                    block[:match.start(2)]
                                    + sch_val
                                    + block[match.end(2):]
                                )
                                content = content[:start] + new_block + content[end + 1:]
                                pcb_p.write_text(content, encoding="utf-8")
                                actions.append({
                                    "type": "value_updated",
                                    "reference": ref,
                                    "old_value": pcb_val,
                                    "new_value": sch_val,
                                })
                            else:
                                warnings.append({
                                    "type": "value_mismatch",
                                    "reference": ref,
                                    "message": f"Could not find Value property in PCB for {ref}.",
                                })
                        else:
                            warnings.append({
                                "type": "value_mismatch",
                                "reference": ref,
                                "message": f"Could not locate {ref} in PCB for value update.",
                            })
                    except Exception as exc:
                        warnings.append({
                            "type": "value_update_failed",
                            "reference": ref,
                            "message": str(exc),
                        })
                else:
                    warnings.append({
                        "type": "value_mismatch",
                        "reference": ref,
                        "schematic_value": sch_val,
                        "pcb_value": pcb_val,
                        "message": f"{ref} value mismatch (board modify not available).",
                    })

        summary = {
            "components_placed": sum(1 for a in actions if a["type"] == "placed"),
            "values_updated": sum(1 for a in actions if a["type"] == "value_updated"),
            "warnings": len(warnings),
        }

        change_log.record(
            "sync_schematic_to_pcb",
            {"schematic_path": schematic_path, "board_path": board_path},
            file_modified=board_path,
        )
        return json.dumps({
            "status": "success",
            "summary": summary,
            "actions": actions,
            "warnings": warnings,
        }, indent=2)

    @mcp.tool()
    def add_junction(path: str, x: float, y: float) -> str:
        """Add a junction dot to the schematic at the given position.

        Junctions indicate that crossing wires are electrically connected.
        Place them at wire intersection points.

        Args:
            path: Path to .kicad_sch file.
            x: X position (at wire intersection).
            y: Y position (at wire intersection).

        Returns:
            JSON with junction position and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_ops()
        result = ops.add_junction(p, x, y)
        change_log.record(
            "add_junction",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)
