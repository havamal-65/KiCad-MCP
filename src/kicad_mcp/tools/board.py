"""PCB board tools - 10 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.response_limit import limit_response
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
    def read_board(path: str, include: list[str] | None = None) -> str:
        """Read a PCB board file and return its complete structure.

        Args:
            path: Path to .kicad_pcb file.
            include: Optional list of sections to return. Omit for all sections.
                     Valid values: components, nets, tracks, vias, zones.
                     The "info" section is always returned regardless of this filter.

        Returns:
            JSON with board info, components, nets, and tracks.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        result = ops.read_board(p)

        VALID = {"components", "nets", "tracks", "vias", "zones"}
        if include:
            keep = set(include) & VALID
            result = {k: v for k, v in result.items() if k == "info" or k in keep}

        change_log.record("read_board", {"path": path})
        return json.dumps({"status": "success", **limit_response(result)}, indent=2)

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

    @mcp.tool()
    def refill_zones(path: str) -> str:
        """Refill all copper pour zones on a PCB board.

        Recalculates copper fill for all zones after component placement or
        routing changes. Requires KiCad to be running (IPC backend).

        Args:
            path: Path to the .kicad_pcb file.

        Returns:
            JSON with refill status.
        """
        board_path = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_zone_refill_ops()
        if ops is None:
            return json.dumps({
                "status": "unavailable",
                "reason": "refill_zones requires KiCad running with IPC",
            })
        result = ops.refill_zones(board_path)
        change_log.record("refill_zones", {"path": path})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def get_stackup(path: str) -> str:
        """Return the layer stackup for a PCB board.

        Retrieves copper, dielectric, and finish layer information.
        Requires KiCad to be running (IPC backend).

        Args:
            path: Path to the .kicad_pcb file.

        Returns:
            JSON with layer stackup details.
        """
        board_path = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_stackup_ops()
        if ops is None:
            return json.dumps({
                "status": "unavailable",
                "reason": "get_stackup requires KiCad running with IPC",
            })
        return json.dumps(ops.get_stackup(board_path), indent=2)

    @mcp.tool()
    def set_board_design_rules(path: str, preset: str = "class2") -> str:
        """Write IPC-2221 design rules into a board's setup section.

        Encodes manufacturing-enforceable design constraints so DRC catches
        real violations rather than using KiCad's permissive defaults.

        Preset "class2" applies IPC-2221 Class 2 / IPC-7351 Level B values:
          min_clearance   0.20 mm  (IPC-2221 Table 6-1, ≤30 V)
          trace_min       0.25 mm  (IPC-2221 / JLCPCB/PCBWay minimum)
          via_min_drill   0.30 mm  (IPC-2221 / common fab minimum)
          via_min_size    0.60 mm  (annular ring ≥ 0.15 mm → pad ≥ 0.60 mm)
          via_min_annulus 0.15 mm  (IPC-2221)
          hole_clearance  0.25 mm  (IPC-2221)
          courtyard_offset 0.25 mm (IPC-7351 Level B)

        Preset "fab_jlcpcb" applies JLCPCB 2-layer standard design rules
        (tighter than IPC-2221 Class 2 in some parameters).

        Args:
            path: Path to .kicad_pcb file.
            preset: Rule preset — "class2" (default) or "fab_jlcpcb".

        Returns:
            JSON with applied preset name and rule values.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        backup = create_backup(p)
        from kicad_mcp.backends.file_backend import FileBoardOps
        result = FileBoardOps().set_board_design_rules(p, preset)
        change_log.record(
            "set_board_design_rules",
            {"path": path, "preset": preset},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_board_outline(
        path: str,
        x: float,
        y: float,
        width: float,
        height: float,
        line_width: float = 0.05,
    ) -> str:
        """Add a rectangular Edge.Cuts board outline to the PCB.

        Inserts a gr_rect graphic on the Edge.Cuts layer, which defines the
        physical board boundary required for fabrication and autorouting.
        Any existing Edge.Cuts gr_rect is replaced.

        Args:
            path: Path to .kicad_pcb file.
            x: Left edge X coordinate in mm (e.g. 3.0 for a 3 mm margin).
            y: Top edge Y coordinate in mm (KiCad Y increases downward).
            width: Board width in mm.
            height: Board height in mm.
            line_width: Outline stroke width in mm (default 0.05).

        Returns:
            JSON with x, y, width, height, x2, y2 of the placed outline.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        validate_positive(width, "width")
        validate_positive(height, "height")
        validate_positive(line_width, "line_width")

        backup = create_backup(p)
        from kicad_mcp.backends.file_backend import FileBoardOps
        result = FileBoardOps().add_board_outline(p, x, y, width, height, line_width)
        change_log.record(
            "add_board_outline",
            {"path": path, "x": x, "y": y, "width": width, "height": height},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def auto_place(
        path: str,
        board_x: float = 3.0,
        board_y: float = 3.0,
        board_width: float = 100.0,
        board_height: float = 80.0,
        clearance_mm: float = 1.5,
    ) -> str:
        """Automatically place all board components using geometry-driven bin-packing.

        Reads the courtyard extents for every footprint, sorts components by
        class (connectors → ICs → discretes → transistors → LEDs → others),
        then bin-packs them into rows within the board outline with a guaranteed
        courtyard-to-courtyard gap ≥ clearance_mm.

        This replaces trial-and-error manual placement and eliminates courtyard
        overlap violations.

        Args:
            path: Path to .kicad_pcb file.
            board_x: Left edge of the placement area in mm (default 3.0).
            board_y: Top edge of the placement area in mm (default 3.0).
            board_width: Width of the placement area in mm (default 100.0).
            board_height: Height of the placement area in mm (default 80.0).
            clearance_mm: Minimum courtyard-to-courtyard gap in mm (default 0.5).

        Returns:
            JSON with components_placed, rows, total_area_mm2, and any warnings.
        """
        from kicad_mcp.backends.file_backend import (
            FileBoardOps,
            _load_kicad_mod,
            _parse_footprint_bounds,
        )

        p = validate_kicad_path(path, ".kicad_pcb")
        board_ops = FileBoardOps()
        components = board_ops.get_components(p)

        if not components:
            return json.dumps({
                "status": "success",
                "message": "No components found on board",
                "components_placed": 0,
                "rows": 0,
            }, indent=2)

        def _component_class_key(ref: str) -> int:
            """Sort order: connectors=0, ICs=1, discretes=2, transistors=3, LEDs=4, other=5."""
            prefix = ''.join(c for c in ref if c.isalpha()).upper()
            order = {"J": 0, "P": 0, "CON": 0, "U": 1, "IC": 1, "R": 2, "C": 2, "L": 2,
                     "Q": 3, "T": 3, "D": 4, "LED": 4}
            for k, v in order.items():
                if prefix.startswith(k):
                    return v
            return 5

        # Sort components by class then reference
        components.sort(key=lambda c: (_component_class_key(c.get("reference", "")),
                                        c.get("reference", "")))

        warnings: list[str] = []
        placements: list[dict] = []

        # Gather dimensions for each component
        comp_dims: list[dict] = []
        for comp in components:
            ref = comp.get("reference", "")
            fp = comp.get("footprint", "")
            if not ref or not fp:
                warnings.append(f"Skipping {ref or '?'}: no footprint assigned")
                continue

            kicad_mod = _load_kicad_mod(fp)
            if kicad_mod is None:
                warnings.append(f"{ref}: footprint '{fp}' not in libraries, using 5×5 mm default")
                w, h, xo, yo = 5.0, 5.0, 2.5, 2.5
            else:
                bounds = _parse_footprint_bounds(kicad_mod)
                w = bounds["width_mm"] if bounds["width_mm"] > 0 else 5.0
                h = bounds["height_mm"] if bounds["height_mm"] > 0 else 5.0
                xo = bounds.get("x_origin", w / 2)
                yo = bounds.get("y_origin", h / 2)

            comp_dims.append({"reference": ref, "footprint": fp, "w": w, "h": h, "xo": xo, "yo": yo})

        if not comp_dims:
            return json.dumps({
                "status": "success",
                "message": "No placeable components found",
                "components_placed": 0,
                "rows": 0,
                "warnings": warnings,
            }, indent=2)

        # Bin-pack into rows
        cursor_x = board_x + clearance_mm
        cursor_y = board_y + clearance_mm
        row_height = 0.0
        row_count = 1
        right_limit = board_x + board_width - clearance_mm
        bottom_limit = board_y + board_height - clearance_mm

        board_modify_ops = FileBoardOps()
        backup = create_backup(p)

        for item in comp_dims:
            w, h = item["w"], item["h"]
            xo, yo = item["xo"], item["yo"]

            # Check if we need to wrap to next row
            if cursor_x + w > right_limit and cursor_x > board_x + clearance_mm:
                cursor_x = board_x + clearance_mm
                cursor_y += row_height + clearance_mm
                row_height = 0.0
                row_count += 1

            if cursor_y + h > bottom_limit:
                warnings.append(
                    f"{item['reference']}: board area full, placed at ({cursor_x:.2f}, {cursor_y:.2f})"
                )

            # Place footprint origin so the courtyard left/top edge lands on the cursor.
            # xo = distance from courtyard left edge to footprint origin (= -courtyard.xmin).
            cx = round(cursor_x + xo, 4)
            cy = round(cursor_y + yo, 4)

            try:
                board_modify_ops.move_component(p, item["reference"], cx, cy, rotation=0.0)
                placements.append({"reference": item["reference"], "x": cx, "y": cy})
                change_log.record(
                    "auto_place_component",
                    {"path": path, "reference": item["reference"], "x": cx, "y": cy},
                    file_modified=path,
                    backup_path=str(backup) if backup else None,
                )
            except Exception as e:
                warnings.append(f"{item['reference']}: move failed — {e}")

            cursor_x += w + clearance_mm
            row_height = max(row_height, h)

        total_area = sum(d["w"] * d["h"] for d in comp_dims)

        return json.dumps({
            "status": "success",
            "components_placed": len(placements),
            "rows": row_count,
            "total_area_mm2": round(total_area, 2),
            "placements": placements,
            "warnings": warnings,
        }, indent=2)

    @mcp.tool()
    def pcb_pipeline(
        schematic_path: str,
        board_path: str,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        design_rules_preset: str = "",
        max_passes: int = 10,
    ) -> str:
        """Run the schematic-to-routed-PCB pipeline in a single call.

        Executes PCB steps in order, with validation between steps:
          1. sync_schematic_to_pcb — place footprints and assign nets
          2. set_board_design_rules — only if design_rules_preset is non-empty
          3. Add Edge.Cuts board outline (gr_rect)
          4. auto_place — geometry-driven component layout, 0.5 mm clearance
          5. autoroute — FreeRouting auto-router
          6. run_drc — design rule validation

        Design rules should be set at project creation via create_project
        (pass design_rules_preset there).  Leave design_rules_preset empty
        here unless working with a project that was created without a preset
        and you need to apply one now.

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.
            board_width_mm: Target board width in mm (default 100).
            board_height_mm: Target board height in mm (default 80).
            design_rules_preset: Re-apply design rules if non-empty
                ("class2" or "fab_jlcpcb"). Leave empty (default) if rules
                were already applied at create_project.
            max_passes: Maximum FreeRouting autorouter passes (default 10).
                Increase for better routing quality; 10 is fast and sufficient
                for validation.

        Returns:
            JSON with status, per-step results, drc_passed, and violations list.
        """
        from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps
        from kicad_mcp.config import KiCadMCPConfig
        from kicad_mcp.tools.routing import (
            _impl_export_dsn,
            _impl_import_ses,
            _impl_run_freerouter,
            _validate_board_preflight,
        )

        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")
        config = KiCadMCPConfig()

        pipeline_steps: list[dict] = []
        overall_status = "success"
        MARGIN = 5.0  # mm between board edge and Edge.Cuts

        def _step(name: str, result_json: str) -> dict:
            r = json.loads(result_json)
            pipeline_steps.append({"step": name, **r})
            return r

        def _fail(name: str, message: str) -> str:
            pipeline_steps.append({"step": name, "status": "error", "message": message})
            return json.dumps({
                "status": "error",
                "failed_step": name,
                "message": message,
                "steps": pipeline_steps,
            }, indent=2)

        # ── Step 1: sync_schematic_to_pcb ────────────────────────────────────
        try:
            sch_ops = FileSchematicOps()
            sch_data = sch_ops.read_schematic(sch_p)

            pcb_ops = FileBoardOps()
            pcb_data = pcb_ops.read_board(pcb_p)

            sch_by_ref: dict[str, dict] = {}
            for sym in sch_data.get("symbols", []):
                ref = sym.get("reference", "")
                if not ref or ref.startswith("#") or sym.get("is_power"):
                    continue
                if ref not in sch_by_ref:
                    sch_by_ref[ref] = sym
                elif sym.get("footprint") and not sch_by_ref[ref].get("footprint"):
                    sch_by_ref[ref]["footprint"] = sym["footprint"]

            pcb_by_ref = {c.get("reference", ""): c for c in pcb_data.get("components", []) if c.get("reference")}

            board_modify = FileBoardOps()
            place_x, place_y = 50.0, 50.0
            placed: list[str] = []
            sync_warnings: list[str] = []

            for ref, sym in sch_by_ref.items():
                fp = sym.get("footprint", "")
                if not fp:
                    sync_warnings.append(f"{ref}: no footprint in schematic, skipped")
                    continue
                if ref not in pcb_by_ref:
                    board_modify.place_component(pcb_p, ref, fp, place_x, place_y)
                    placed.append(ref)
                    place_x += 10.0
                    if place_x > 200.0:
                        place_x = 50.0
                        place_y += 10.0

            # Assign nets from schematic connectivity to PCB pads
            connectivity = sch_ops._build_connectivity(sch_p)
            net_assign_count = 0
            net_assign_warnings: list[str] = []
            board_assign = FileBoardOps()
            for net_name, pins in connectivity.items():
                if not net_name:
                    continue
                for pin in pins:
                    pin_ref = pin.get("reference", "")
                    pad = str(pin.get("pin_number", ""))
                    if not pin_ref or not pad:
                        continue
                    try:
                        board_assign.assign_net(pcb_p, pin_ref, pad, net_name)
                        net_assign_count += 1
                    except Exception as net_exc:
                        net_assign_warnings.append(f"{pin_ref}/{pad}: {net_exc}")

            pipeline_steps.append({
                "step": "sync_schematic_to_pcb",
                "status": "success",
                "placed": placed,
                "nets_assigned": net_assign_count,
                "warnings": sync_warnings + net_assign_warnings,
            })
            change_log.record("pcb_pipeline_sync", {"schematic": schematic_path, "board": board_path})
        except Exception as exc:
            return _fail("sync_schematic_to_pcb", str(exc))

        # ── Step 2: set_board_design_rules (only if explicitly requested) ────
        # Rules should already be set by create_project; only re-apply here
        # if the caller explicitly passes a non-empty preset (e.g. migrating
        # an older project that was created without a preset).
        if design_rules_preset:
            try:
                dr_result = FileBoardOps().set_board_design_rules(pcb_p, design_rules_preset)
                pipeline_steps.append({
                    "step": "set_board_design_rules",
                    "status": "success",
                    **dr_result,
                })
            except Exception as exc:
                return _fail("set_board_design_rules", str(exc))
        else:
            pipeline_steps.append({
                "step": "set_board_design_rules",
                "status": "skipped",
                "note": "Design rules already applied at create_project.",
            })

        # ── Step 3: add Edge.Cuts board outline ──────────────────────────────
        # Delegates to FileBoardOps.add_board_outline, which removes any
        # existing gr_rect on Edge.Cuts before inserting — safe on reruns.
        try:
            x1 = round(MARGIN, 4)
            y1 = round(MARGIN, 4)
            FileBoardOps().add_board_outline(pcb_p, x1, y1, board_width_mm, board_height_mm)
            pipeline_steps.append({
                "step": "add_board_outline",
                "status": "success",
                "outline": {
                    "x1": x1, "y1": y1,
                    "x2": round(x1 + board_width_mm, 4),
                    "y2": round(y1 + board_height_mm, 4),
                },
                "layer": "Edge.Cuts",
            })
        except Exception as exc:
            return _fail("add_board_outline", str(exc))

        # ── Step 4: auto_place ───────────────────────────────────────────────
        try:
            from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

            components = FileBoardOps().get_components(pcb_p)
            components.sort(key=lambda c: (
                {"J": 0, "P": 0, "U": 1, "IC": 1, "R": 2, "C": 2, "L": 2,
                 "Q": 3, "T": 3, "D": 4}.get(
                    ''.join(ch for ch in c.get("reference", "") if ch.isalpha()).upper()[:2], 5
                ),
                c.get("reference", ""),
            ))

            ap_clearance = 1.5
            cx_cur = MARGIN + ap_clearance
            cy_cur = MARGIN + ap_clearance
            row_h = 0.0
            row_n = 1
            right_lim = MARGIN + board_width_mm - ap_clearance
            bot_lim = MARGIN + board_height_mm - ap_clearance
            ap_warnings: list[str] = []
            ap_placed: list[str] = []
            board_mv = FileBoardOps()

            for comp in components:
                ref = comp.get("reference", "")
                fp = comp.get("footprint", "")
                if not ref or not fp:
                    continue
                kicad_mod = _load_kicad_mod(fp)
                if kicad_mod:
                    b = _parse_footprint_bounds(kicad_mod)
                    w = b["width_mm"] if b["width_mm"] > 0 else 5.0
                    h = b["height_mm"] if b["height_mm"] > 0 else 5.0
                    xo = b.get("x_origin", w / 2)
                    yo = b.get("y_origin", h / 2)
                else:
                    w, h = 5.0, 5.0
                    xo, yo = 2.5, 2.5
                    ap_warnings.append(f"{ref}: footprint not in libraries, using 5×5 mm")

                if cx_cur + w > right_lim and cx_cur > MARGIN + ap_clearance:
                    cx_cur = MARGIN + ap_clearance
                    cy_cur += row_h + ap_clearance
                    row_h = 0.0
                    row_n += 1

                comp_cx = round(cx_cur + xo, 4)
                comp_cy = round(cy_cur + yo, 4)
                try:
                    board_mv.move_component(pcb_p, ref, comp_cx, comp_cy, rotation=0.0)
                    ap_placed.append(ref)
                except Exception as mv_exc:
                    ap_warnings.append(f"{ref}: move failed — {mv_exc}")

                cx_cur += w + ap_clearance
                row_h = max(row_h, h)

            pipeline_steps.append({
                "step": "auto_place",
                "status": "success",
                "components_placed": len(ap_placed),
                "rows": row_n,
                "warnings": ap_warnings,
            })
        except Exception as exc:
            return _fail("auto_place", str(exc))

        # ── Step 5: autoroute ────────────────────────────────────────────────
        # Skip _validate_board_preflight: the outline was written in Step 3.
        # Spawning a KiCad Python subprocess just to re-verify it would add
        # 10-30 s of startup overhead on Windows without benefit.
        try:
            dsn = pcb_p.parent / "freerouting.dsn"
            ses = pcb_p.parent / "freerouting.ses"

            dsn_result = json.loads(
                _impl_export_dsn(str(pcb_p), str(dsn), config, change_log)
            )
            if dsn_result.get("status") != "success":
                return _fail("autoroute_export_dsn", dsn_result.get("message", "DSN export failed"))

            router_result = json.loads(
                _impl_run_freerouter(str(dsn), str(ses), max_passes, "", "", config, change_log)
            )
            if router_result.get("status") != "success":
                return _fail("autoroute_freerouter", router_result.get("message", "FreeRouting failed"))

            ses_result = json.loads(
                _impl_import_ses(str(pcb_p), str(ses), change_log)
            )
            if ses_result.get("status") != "success":
                return _fail("autoroute_import_ses", ses_result.get("message", "SES import failed"))

            pipeline_steps.append({
                "step": "autoroute",
                "status": "success",
                "router": router_result,
            })
        except Exception as exc:
            return _fail("autoroute", str(exc))

        # ── Step 6: run_drc ──────────────────────────────────────────────────
        drc_passed = False
        violations: list = []
        try:
            drc_ops = backend.get_drc_ops()
            drc_result = drc_ops.run_drc(pcb_p, None)
            drc_passed = drc_result.get("passed", False)
            violations = drc_result.get("violations", [])
            pipeline_steps.append({
                "step": "run_drc",
                "status": "success",
                "passed": drc_passed,
                "error_count": drc_result.get("error_count", 0),
                "warning_count": drc_result.get("warning_count", 0),
            })
        except Exception as exc:
            pipeline_steps.append({
                "step": "run_drc",
                "status": "unavailable",
                "message": f"DRC requires kicad-cli: {exc}",
            })
            overall_status = "success_no_drc"

        change_log.record("pcb_pipeline", {"schematic": schematic_path, "board": board_path})

        return json.dumps({
            "status": overall_status,
            "board_path": str(pcb_p),
            "drc_passed": drc_passed,
            "violations": violations[:20],  # cap to avoid huge responses
            "steps": pipeline_steps,
        }, indent=2)
