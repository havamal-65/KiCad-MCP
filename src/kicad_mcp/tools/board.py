"""PCB board tools - 12 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
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


# ── §6.5 board-size verification ─────────────────────────────────────────────

def run_verify_board_size(
    board_path: Path,
    *,
    panel_keepout_mm: float = 3.0,
    mounting_hole_keepout_mm: float = 3.0,
    fiducial_keepout_mm: float = 1.0,
    routing_channel_pct: float = 20.0,
) -> dict:
    """Verify the board outline accommodates all placed parts plus manufacturing
    tolerances. Read-only. Records {passed, shortfall_breakdown,
    suggested_min_dimensions} to the validation cache so auto_place can gate on
    it (§6.5). Returns the full verdict dict.
    """
    import math

    from kicad_mcp.backends.file_backend import FileBoardOps
    from kicad_mcp.tools.drc import _parse_board_bbox, _parse_placed_courtyards
    from kicad_mcp.utils.board_size import (
        BoardSizeTolerances,
        is_fiducial,
        is_mounting_hole,
        suggest_dimensions,
    )
    from kicad_mcp.utils.validation_cache import record_validation

    tol = BoardSizeTolerances(
        panel_keepout_mm=panel_keepout_mm,
        mounting_hole_keepout_mm=mounting_hole_keepout_mm,
        fiducial_keepout_mm=fiducial_keepout_mm,
        routing_channel_pct=routing_channel_pct,
    )
    content = board_path.read_text(encoding="utf-8")

    # Outline (REQ-CHECK-001) — no Edge.Cuts is a hard fail, not a crash.
    bbox = _parse_board_bbox(content)
    if bbox is None:
        sb = {"reason": "no_board_outline"}
        result = {
            "passed": False,
            "shortfall_breakdown": sb,
            "message": (
                "No Edge.Cuts outline found. Add a board outline "
                "(add_board_outline) before verifying size."
            ),
            "warnings": [],
        }
        record_validation(
            board_path, "verify_board_size",
            {"passed": False, "shortfall_breakdown": sb, "suggested_min_dimensions": {}},
        )
        return result

    xmin, ymin, xmax, ymax = bbox
    width, height = xmax - xmin, ymax - ymin
    outline_area = width * height

    courtyards, no_courtyard = _parse_placed_courtyards(content)
    warnings: list[dict] = []

    # Per-part dimensions; footprints with no courtyard contribute a 5x5 default.
    parts: list[tuple[str, float, float]] = [
        (ref, c["xmax"] - c["xmin"], c["ymax"] - c["ymin"])
        for ref, c in courtyards.items()
    ]
    for ref in no_courtyard:
        parts.append((ref, 5.0, 5.0))
        warnings.append({
            "ref": ref, "type": "no_courtyard",
            "message": f"{ref}: no courtyard, assumed 5x5mm",
        })

    courtyard_area = sum(w * h for _, w, h in parts)

    # Mounting-hole / fiducial keepout discs (classification needs the lib_id).
    components = FileBoardOps(project_dir=str(board_path.parent)).get_components(board_path)
    mh = sum(1 for c in components
             if is_mounting_hole(c.get("reference", ""), c.get("footprint", "")))
    fid = sum(1 for c in components
              if is_fiducial(c.get("reference", ""), c.get("footprint", "")))
    mh_area = mh * math.pi * tol.mounting_hole_keepout_mm ** 2
    fid_area = fid * math.pi * tol.fiducial_keepout_mm ** 2

    k = tol.panel_keepout_mm
    inner_w = max(0.0, width - 2 * k)
    inner_h = max(0.0, height - 2 * k)
    edge_band_area = outline_area - inner_w * inner_h
    usable = max(0.0, outline_area - edge_band_area - mh_area - fid_area)
    required = courtyard_area * (1 + tol.routing_channel_pct / 100.0)

    area_ok = required <= usable

    # Dimensional check (REQ-CHECK-005): the single largest part must fit.
    if parts:
        max_w = max(w for _, w, _ in parts)
        max_h = max(h for _, _, h in parts)
        big_ref, big_w, big_h = max(parts, key=lambda p: p[1] * p[2])
    else:
        max_w = max_h = big_w = big_h = 0.0
        big_ref = None
    dim_ok = (width >= max_w + 2 * k) and (height >= max_h + 2 * k)

    passed = area_ok and dim_ok

    aspect = width / height if height > 0 else 1.4
    suggested = suggest_dimensions(required, max_w, max_h, mh_area, fid_area, tol, aspect)

    if passed and usable > 0 and required / usable > tol.utilization_ceiling_pct / 100.0:
        warnings.append({
            "type": "high_utilization",
            "utilization_pct": round(100 * required / usable, 1),
            "message": "Board is tight; less than 20% area margin remains.",
        })

    shortfall = {
        "outline_mm2": round(outline_area, 1),
        "edge_keepout_mm2": round(edge_band_area, 1),
        "mounting_hole_keepout_mm2": round(mh_area, 1),
        "fiducial_keepout_mm2": round(fid_area, 1),
        "courtyard_mm2": round(courtyard_area, 1),
        "routing_channel_mm2": round(required - courtyard_area, 1),
        "required_mm2": round(required, 1),
        "usable_mm2": round(usable, 1),
        "shortfall_mm2": round(max(0.0, required - usable), 1),
    }
    if big_ref is not None:
        shortfall["largest_part"] = {
            "ref": big_ref,
            "width_mm": round(big_w, 2),
            "height_mm": round(big_h, 2),
        }

    result = {
        "passed": passed,
        "parts_counted": len(parts),
        "total_required_mm2": round(required, 1),
        "available_mm2": round(outline_area, 1),
        "usable_mm2": round(usable, 1),
        "shortfall_breakdown": shortfall,
        "suggested_min_dimensions": suggested,
        "warnings": warnings,
    }
    record_validation(
        board_path, "verify_board_size",
        {"passed": passed, "shortfall_breakdown": shortfall,
         "suggested_min_dimensions": suggested},
    )
    return result


def _board_size_refusal_message(cached: dict) -> str:
    """Build the auto_place refusal message from a cached verify_board_size
    failure (REQ-DIAG-003: shortfall + suggested dims inline)."""
    sb = cached.get("shortfall_breakdown", {})
    if sb.get("reason") == "no_board_outline":
        return (
            "auto_place blocked: the board has no Edge.Cuts outline. Add one "
            "(add_board_outline), then run verify_board_size(path)."
        )
    sug = cached.get("suggested_min_dimensions", {})
    msg = (
        "auto_place blocked: verify_board_size failed — required "
        f"{sb.get('required_mm2')} mm² exceeds usable {sb.get('usable_mm2')} mm² "
        f"(short {sb.get('shortfall_mm2')} mm²)."
    )
    big = sb.get("largest_part")
    if big:
        msg += f" Largest part {big['ref']} is {big['width_mm']}×{big['height_mm']} mm."
    if sug:
        msg += (
            f" Enlarge the board to at least {sug.get('width_mm')}×"
            f"{sug.get('height_mm')} mm (add_board_outline), then re-verify."
        )
    return msg


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
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
    def place_at_edge(
        path: str,
        reference: str,
        edge: str,
        offset_mm: float = 2.0,
    ) -> str:
        """Anchor an edge-facing connector at the named board edge, facing outward.

        Reads the connector's local-frame mating face (via the same detection
        the placement-quality gate uses), computes the rotation that makes
        that face point outward at the requested edge, and positions the
        footprint so its courtyard sits offset_mm inside the Edge.Cuts
        outline. The footprint is centered along the edge by default.

        Use this in /build-pcb Phase 4b to anchor connectors before
        auto_place runs. The footprints anchored here should be passed to
        auto_place via its anchors parameter so bulk placement doesn't
        disturb them.

        Args:
            path: Path to .kicad_pcb file.
            reference: Reference designator of the connector to place (e.g. J1).
            edge: One of "north", "south", "east", "west".
            offset_mm: Clearance from courtyard to Edge.Cuts (default 2.0).

        Returns:
            JSON with target position {x, y, rotation} and the mating-face
            evidence used to compute it. The footprint has been moved in place.
        """
        from kicad_mcp.tools.drc import compute_edge_placement

        p = validate_kicad_path(path, ".kicad_pcb")
        validate_reference(reference)

        plan = compute_edge_placement(p, reference, edge, offset_mm)
        if plan["status"] != "success":
            return json.dumps(plan)

        backup = create_backup(p)
        ops = backend.get_board_modify_ops()
        result = ops.move_component(
            p, reference, plan["target_x"], plan["target_y"], plan["target_rotation"]
        )
        change_log.record(
            "place_at_edge",
            {
                "path": path,
                "reference": reference,
                "edge": edge,
                "x": plan["target_x"],
                "y": plan["target_y"],
                "rotation": plan["target_rotation"],
            },
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({
            "status": "success",
            "reference": reference,
            "edge": edge,
            "target_x": round(plan["target_x"], 4),
            "target_y": round(plan["target_y"], 4),
            "target_rotation": plan["target_rotation"],
            "local_mating_face": plan["local_mating_face"],
            "evidence": plan["evidence"],
            **result,
        }, indent=2)

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
        result = backend.get_board_modify_ops().add_board_outline(p, x, y, width, height, line_width)
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
        anchors: list[str] | None = None,
    ) -> str:
        """Automatically place all board components using geometry-driven bin-packing.

        Reads the courtyard extents for every footprint, sorts components by
        class (connectors → ICs → discretes → transistors → LEDs → others),
        then bin-packs them into rows within the board outline with a guaranteed
        courtyard-to-courtyard gap ≥ clearance_mm.

        This replaces trial-and-error manual placement and eliminates courtyard
        overlap violations.

        Use the anchors parameter to keep specific refs in place — typically
        connectors that place_at_edge has already anchored at the board edge.
        Anchored refs are skipped during bulk placement but their courtyards
        are not considered as obstacles (a known simplification — pass them
        a generous clearance_mm if bulk placement crowds the edges).

        Args:
            path: Path to .kicad_pcb file.
            board_x: Left edge of the placement area in mm (default 3.0).
            board_y: Top edge of the placement area in mm (default 3.0).
            board_width: Width of the placement area in mm (default 100.0).
            board_height: Height of the placement area in mm (default 80.0).
            clearance_mm: Minimum courtyard-to-courtyard gap in mm (default 0.5).
            anchors: Optional list of refs whose positions must not be modified
                (e.g. ["J1", "J2", "J3"] for connectors placed by place_at_edge).

        Returns:
            JSON with components_placed, rows, total_area_mm2, and any warnings.
        """
        p = validate_kicad_path(path, ".kicad_pcb")

        # ── §6.5 board-size gate ─────────────────────────────────────────────
        # Recorded verify_board_size FAILURE → hard refuse (placing into a board
        # too small for its parts is futile). Never verified → proceed, but warn
        # (placement is iterative/cheap; an unverified board is a nudge, not a
        # block — mirrors the §6.3 sync warn-gate, contrast autoroute).
        from kicad_mcp.utils.gates import check_gate
        from kicad_mcp.utils.validation_cache import get_validation
        gate_warnings: list[dict] = []
        gap = check_gate(p, "verify_board_size")
        if gap is not None and gap["ran"] and not gap["passed"]:
            cached = get_validation(p, "verify_board_size") or {}
            return json.dumps({
                "status": "blocked",
                "reason": "verify_board_size_gate",
                "shortfall_breakdown": cached.get("shortfall_breakdown", {}),
                "suggested_min_dimensions": cached.get("suggested_min_dimensions", {}),
                "message": _board_size_refusal_message(cached),
            }, indent=2)
        if gap is not None and not gap["ran"]:
            gate_warnings.append({
                "type": "board_size_unverified",
                "message": "Run verify_board_size(board_path) before placement.",
            })

        board_ops = backend.get_board_modify_ops()

        if not board_ops.get_components(p):
            return json.dumps({
                "status": "success",
                "message": "No components found on board",
                "components_placed": 0,
                "rows": 0,
            }, indent=2)

        backup = create_backup(p)
        result = board_ops.auto_place(
            p, board_x, board_y, board_width, board_height, clearance_mm,
            anchors=anchors,
        )

        for placement in result.get("placements", []):
            change_log.record(
                "auto_place_component",
                {"path": path, **placement},
                file_modified=path,
                backup_path=str(backup) if backup else None,
            )

        # Compute board utilization and warn if routing will be difficult
        board_area = board_width * board_height
        total_courtyard_area = result.get("total_area_mm2", 0.0)
        utilization_pct = round((total_courtyard_area / board_area * 100) if board_area > 0 else 0.0, 1)
        result["utilization_pct"] = utilization_pct
        if utilization_pct > 70:
            result.setdefault("warnings", []).append(
                f"Board utilization is {utilization_pct}% (>{70}%). Routing will be extremely difficult. "
                "Consider enlarging the board with add_board_outline before autoroute."
            )

        if gate_warnings:
            result.setdefault("warnings", []).extend(gate_warnings)

        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def verify_board_size(
        board_path: str,
        panel_keepout_mm: float = 3.0,
        mounting_hole_keepout_mm: float = 3.0,
        fiducial_keepout_mm: float = 1.0,
        routing_channel_pct: float = 20.0,
    ) -> str:
        """Verify the board outline fits all placed parts plus fab tolerances.

        Run after parts are synced to the PCB and before auto_place. Computes
        usable area (board outline minus panelization edge keepout, mounting-hole
        and fiducial keepout discs) versus required area (component courtyards +
        routing channels), plus a single-largest-part dimensional check. A
        recorded failure blocks auto_place. Read-only; records its verdict to the
        validation cache.

        Args:
            board_path: Path to .kicad_pcb file.
            panel_keepout_mm: Panelization edge keepout band, all edges (default 3.0).
            mounting_hole_keepout_mm: Keepout radius per mounting hole (default 3.0).
            fiducial_keepout_mm: Keepout radius per fiducial (default 1.0).
            routing_channel_pct: Routing area as % of courtyard area (default 20.0).

        Returns:
            JSON: {passed, parts_counted, total_required_mm2, available_mm2,
            usable_mm2, shortfall_breakdown, suggested_min_dimensions, warnings}.
        """
        p = validate_kicad_path(board_path, ".kicad_pcb")
        result = run_verify_board_size(
            p,
            panel_keepout_mm=panel_keepout_mm,
            mounting_hole_keepout_mm=mounting_hole_keepout_mm,
            fiducial_keepout_mm=fiducial_keepout_mm,
            routing_channel_pct=routing_channel_pct,
        )
        change_log.record("verify_board_size", {"board_path": board_path, "passed": result["passed"]})
        return json.dumps(result, indent=2)

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
        from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps  # FileBoardOps for set_board_design_rules (.kicad_pro file writes)
        from kicad_mcp.config import KiCadMCPConfig
        from kicad_mcp.tools.routing import _impl_run_freerouter

        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")
        config = KiCadMCPConfig()

        pipeline_steps: list[dict] = []
        overall_status = "success"

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

        # ── Step 0a: startup gate ────────────────────────────────────────────
        try:
            from kicad_mcp.tools.project import run_startup_checklist
            checklist = run_startup_checklist()
            pipeline_steps.append({"step": "startup_checklist", **checklist})
            if not checklist["ready_for_pcb"]:
                failed_items = [c["item"] for c in checklist["checklist"] if c["status"] == "FAIL"]
                return _fail(
                    "startup_checklist",
                    f"Startup gate failed — fix these items before running the pipeline: "
                    f"{', '.join(failed_items)}. Required actions: "
                    f"{'; '.join(checklist['required_actions'])}",
                )
        except Exception as exc:
            # Startup check failure is non-fatal if we can't import the module
            pipeline_steps.append({
                "step": "startup_checklist",
                "status": "skipped",
                "note": f"Startup check skipped: {exc}",
            })

        # ── Step 0b: schematic completeness check ────────────────────────────
        try:
            from kicad_mcp.tools.drc import run_validate_schematic_for_pcb
            sch_check = run_validate_schematic_for_pcb(sch_p)
            pipeline_steps.append({"step": "validate_schematic_for_pcb", **sch_check})
            if not sch_check["ready_for_pcb_sync"]:
                issues = [i.get("detail", str(i)) for i in sch_check["blocking_issues"][:5]]
                return _fail(
                    "validate_schematic_for_pcb",
                    f"Schematic has {len(sch_check['blocking_issues'])} blocking issue(s). "
                    f"First issues: {'; '.join(issues)}",
                )
        except Exception as exc:
            pipeline_steps.append({
                "step": "validate_schematic_for_pcb",
                "status": "skipped",
                "note": f"Schematic check skipped: {exc}",
            })

        # ── Step 0c: board size estimate warning ─────────────────────────────
        if board_width_mm > 0 and board_height_mm > 0:
            try:
                from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds
                import math as _math
                from kicad_mcp.backends.file_backend import FileSchematicOps as _FSO
                _sch_data = _FSO().read_schematic(sch_p)
                _fp_ids = [
                    s.get("footprint", "")
                    for s in _sch_data.get("symbols", [])
                    if not s.get("is_power") and s.get("footprint")
                ]
                _comp_area = 0.0
                for _fp_id in _fp_ids:
                    _mod = _load_kicad_mod(_fp_id)
                    if _mod:
                        _b = _parse_footprint_bounds(_mod)
                        _comp_area += (_b["width_mm"] or 5.0) * (_b["height_mm"] or 5.0)
                if _comp_area > 0:
                    _ra = _comp_area * 1.20  # 20% routing overhead
                    _ew = _math.sqrt(_ra * 1.4) + 6  # + 2×edge_clearance
                    _eh = _math.sqrt(_ra / 1.4) + 6
                    _ceil5 = lambda v: _math.ceil(v / 5.0) * 5.0
                    _est_w = _ceil5(_ceil5(_ew) * 1.25)
                    _est_h = _ceil5(_ceil5(_eh) * 1.25)
                    if board_width_mm < _est_w * 0.85 or board_height_mm < _est_h * 0.85:
                        pipeline_steps.append({
                            "step": "board_size_check",
                            "status": "warning",
                            "message": (
                                f"Provided board size ({board_width_mm}×{board_height_mm} mm) "
                                f"may be too small. Estimate: {_est_w}×{_est_h} mm. "
                                "Routing may fail. Consider enlarging."
                            ),
                        })
                    else:
                        pipeline_steps.append({
                            "step": "board_size_check",
                            "status": "ok",
                            "estimated_size_mm": f"{_est_w}×{_est_h}",
                        })
            except Exception:
                pass  # Size check is best-effort

        # ── Step 1: sync_schematic_to_pcb ────────────────────────────────────
        try:
            sch_ops = FileSchematicOps()
            sch_data = sch_ops.read_schematic(sch_p)

            pcb_ops = backend.get_board_ops()
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

            board_modify = backend.get_board_modify_ops()
            place_x, place_y = 50.0, 50.0
            placed: list[str] = []
            sync_warnings: list[str] = []
            to_place: list[dict] = []

            for ref, sym in sch_by_ref.items():
                fp = sym.get("footprint", "")
                if not fp:
                    sync_warnings.append(f"{ref}: no footprint in schematic, skipped")
                    continue
                if ref not in pcb_by_ref:
                    to_place.append({"reference": ref, "footprint": fp, "x": place_x, "y": place_y})
                    place_x += 10.0
                    if place_x > 200.0:
                        place_x = 50.0
                        place_y += 10.0

            # Place all new components in one operation if supported
            if to_place:
                try:
                    bulk_result = board_modify.place_components_bulk(pcb_p, to_place)
                    placed = bulk_result.get("placed", [])
                    for f in bulk_result.get("failed", []):
                        sync_warnings.append(f"{f.get('reference', '?')}: place failed — {f.get('reason', '')}")
                except NotImplementedError:
                    # Fallback: per-component placement
                    for comp in to_place:
                        board_modify.place_component(
                            pcb_p, comp["reference"], comp["footprint"],
                            comp["x"], comp["y"],
                        )
                        placed.append(comp["reference"])

            # Assign nets from schematic connectivity to PCB pads
            connectivity = sch_ops._build_connectivity(sch_p)
            net_assign_count = 0
            net_assign_warnings: list[str] = []
            board_assign = backend.get_board_modify_ops()
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
        # Centre the board at the KiCad canvas origin (0, 0) so it appears in
        # the middle of the work area rather than near the top-left corner.
        try:
            x1 = round(-board_width_mm / 2, 4)
            y1 = round(-board_height_mm / 2, 4)
            backend.get_board_modify_ops().add_board_outline(pcb_p, x1, y1, board_width_mm, board_height_mm)
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
            ap_result = backend.get_board_modify_ops().auto_place(
                pcb_p, x1, y1, board_width_mm, board_height_mm, 1.5
            )
            pipeline_steps.append({
                "step": "auto_place",
                "status": "success",
                "components_placed": ap_result["components_placed"],
                "rows": ap_result["rows"],
                "warnings": ap_result.get("warnings", []),
            })
        except Exception as exc:
            return _fail("auto_place", str(exc))

        # ── Step 4b: courtyard overlap check ────────────────────────────────
        # CLAUDE.md hard rule: NEVER start routing with courtyard overlaps present.
        try:
            from kicad_mcp.tools.drc import run_check_courtyard_overlaps
            cy_result = run_check_courtyard_overlaps(pcb_p)
            pipeline_steps.append({
                "step": "courtyard_overlaps",
                "status": "success",
                "passed": cy_result["passed"],
                "footprints_checked": cy_result["footprints_checked"],
                "overlap_count": cy_result["overlap_count"],
                "overlaps": cy_result["overlaps"],
            })
            if not cy_result["passed"]:
                overlap_list = cy_result["overlaps"][:5]
                return _fail(
                    "courtyard_overlaps",
                    f"{cy_result['overlap_count']} courtyard overlap(s) detected. "
                    "Call move_component to resolve each conflict, then retry pcb_pipeline. "
                    f"First overlaps: {overlap_list}",
                )
        except Exception as exc:
            return _fail("courtyard_overlaps", str(exc))

        # ── Step 4c: connector orientation gate (Phase 6.1.4) ────────────────
        # autoroute will refuse to start if this validation hasn't passed for
        # the current board state. Run it now so the pipeline either passes
        # cleanly or fails here with actionable remediation.
        try:
            from kicad_mcp.tools.drc import run_validate_connector_orientations
            orient_result = run_validate_connector_orientations(pcb_p)
            pipeline_steps.append({
                "step": "connector_orientations",
                "status": "success",
                "passed": orient_result["passed"],
                "checked": orient_result["checked"],
                "violation_count": len(orient_result["violations"]),
                "violations": orient_result["violations"],
                "indeterminate": orient_result["indeterminate"],
            })
            if not orient_result["passed"]:
                v_brief = [
                    {
                        "ref": v.get("ref"),
                        "suggested_edge": v.get("suggested_edge"),
                        "suggested_rotation": v.get("suggested_rotation"),
                    }
                    for v in orient_result["violations"]
                    if "ref" in v
                ][:5]
                return _fail(
                    "connector_orientations",
                    f"{len(orient_result['violations'])} connector(s) face inward. "
                    "Call place_at_edge(path, ref, suggested_edge) for each, "
                    "then retry pcb_pipeline. "
                    f"First violations: {v_brief}",
                )
        except Exception as exc:
            return _fail("connector_orientations", str(exc))

        # ── Step 5: autoroute ────────────────────────────────────────────────
        # DSN/SES route via BOARD_ROUTE capability (plugin bridge if active,
        # subprocess pcbnew otherwise). Preflight is done inside export_dsn.
        try:
            dsn = pcb_p.parent / "freerouting.dsn"
            ses = pcb_p.parent / "freerouting.ses"

            try:
                backend.export_dsn(pcb_p, dsn)
            except Exception as exc:
                return _fail("autoroute_export_dsn", str(exc))

            router_result = json.loads(
                _impl_run_freerouter(str(dsn), str(ses), max_passes, "", "", config, change_log)
            )
            if router_result.get("status") != "success":
                return _fail("autoroute_freerouter", router_result.get("message", "FreeRouting failed"))

            try:
                backend.import_ses(pcb_p, ses)
            except Exception as exc:
                return _fail("autoroute_import_ses", str(exc))

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

    @mcp.tool()
    def diff_board(board_path_a: str, board_path_b: str) -> str:
        """Detect changes between two PCB board snapshots.

        Compares component positions and track counts between two .kicad_pcb files.
        Useful for verifying what changed after auto_place, autoroute, or any board modification.

        Args:
            board_path_a: Path to the first (original) .kicad_pcb file.
            board_path_b: Path to the second (modified) .kicad_pcb file.

        Returns:
            JSON with added, removed, and moved components plus track count delta.
        """
        from kicad_mcp.backends.file_backend import FileBoardOps
        import math

        pa = validate_kicad_path(board_path_a, ".kicad_pcb")
        pb = validate_kicad_path(board_path_b, ".kicad_pcb")

        board_ops = FileBoardOps()
        comps_a = {c["reference"]: c for c in board_ops.get_components(pa) if c.get("reference")}
        comps_b = {c["reference"]: c for c in board_ops.get_components(pb) if c.get("reference")}
        tracks_a = board_ops.get_tracks(pa)
        tracks_b = board_ops.get_tracks(pb)

        MOVE_THRESHOLD_MM = 0.01  # positions differing by less than this are considered identical

        added = []
        removed = []
        moved = []

        for ref, comp in comps_b.items():
            if ref not in comps_a:
                added.append({
                    "reference": ref,
                    "footprint": comp.get("footprint", ""),
                    "position": comp.get("position", {}),
                })

        for ref, comp in comps_a.items():
            if ref not in comps_b:
                removed.append({
                    "reference": ref,
                    "footprint": comp.get("footprint", ""),
                    "position": comp.get("position", {}),
                })
            else:
                pos_a = comp.get("position", {})
                pos_b = comps_b[ref].get("position", {})
                dx = pos_b.get("x", 0) - pos_a.get("x", 0)
                dy = pos_b.get("y", 0) - pos_a.get("y", 0)
                delta = math.sqrt(dx * dx + dy * dy)
                if delta > MOVE_THRESHOLD_MM:
                    moved.append({
                        "reference": ref,
                        "from": pos_a,
                        "to": pos_b,
                        "delta_mm": round(delta, 4),
                    })

        track_delta = len(tracks_b) - len(tracks_a)

        summary = {
            "added": len(added),
            "removed": len(removed),
            "moved": len(moved),
            "track_delta": track_delta,
        }

        change_log.record("diff_board", {
            "board_path_a": board_path_a,
            "board_path_b": board_path_b,
        })
        return json.dumps({
            "status": "success",
            "summary": summary,
            "added_components": added,
            "removed_components": removed,
            "moved_components": moved,
            "track_delta": track_delta,
        }, indent=2)
