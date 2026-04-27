"""Component library tools - 6 tools."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog

logger = get_logger("tools.library")


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register component library tools on the MCP server."""

    @mcp.tool()
    def search_symbols(query: str, limit: int = 25) -> str:
        """Search for schematic symbols across installed KiCad libraries.

        Args:
            query: Search text to match against symbol names (e.g. 'ATmega', 'resistor', 'LM7805').
            limit: Maximum number of results to return (default 25).

        Returns:
            JSON with matching symbols (name, library, lib_id).
        """
        ops = backend.get_library_ops()
        results = ops.search_symbols(query)
        change_log.record("search_symbols", {"query": query})
        return json.dumps({
            "status": "success",
            "query": query,
            "count": len(results[:limit]),
            "symbols": results[:limit],
        }, indent=2)

    @mcp.tool()
    def search_footprints(query: str, limit: int = 25) -> str:
        """Search for PCB footprints across installed KiCad libraries.

        Args:
            query: Search text to match against footprint names (e.g. 'SOIC-8', 'QFP', '0805').
            limit: Maximum number of results to return (default 25).

        Returns:
            JSON with matching footprints (name, library, lib_id).
        """
        ops = backend.get_library_ops()
        results = ops.search_footprints(query)
        change_log.record("search_footprints", {"query": query})
        return json.dumps({
            "status": "success",
            "query": query,
            "count": len(results[:limit]),
            "footprints": results[:limit],
        }, indent=2)

    @mcp.tool()
    def list_libraries() -> str:
        """List all available KiCad symbol and footprint libraries.

        Returns:
            JSON with library names, types, and paths.
        """
        ops = backend.get_library_ops()
        libraries = ops.list_libraries()
        change_log.record("list_libraries", {})

        symbol_libs = [l for l in libraries if l.get("type") == "symbol"]
        footprint_libs = [l for l in libraries if l.get("type") == "footprint"]

        return json.dumps({
            "status": "success",
            "total": len(libraries),
            "symbol_libraries": len(symbol_libs),
            "footprint_libraries": len(footprint_libs),
            "libraries": libraries,
        }, indent=2)

    @mcp.tool()
    def get_symbol_info(lib_id: str) -> str:
        """Get detailed information about a specific symbol.

        Args:
            lib_id: Symbol identifier in 'Library:Symbol' format (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').

        Returns:
            JSON with symbol details: description, pins, properties.
        """
        ops = backend.get_library_ops()
        result = ops.get_symbol_info(lib_id)
        change_log.record("get_symbol_info", {"lib_id": lib_id})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_footprint_info(lib_id: str) -> str:
        """Get detailed information about a specific footprint.

        Args:
            lib_id: Footprint identifier in 'Library:Footprint' format (e.g. 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm').

        Returns:
            JSON with footprint details: description, pads, dimensions.
        """
        ops = backend.get_library_ops()
        result = ops.get_footprint_info(lib_id)
        change_log.record("get_footprint_info", {"lib_id": lib_id})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def suggest_footprints(lib_id: str) -> str:
        """Suggest matching footprints for a symbol based on its footprint filters.

        Reads the symbol's ki_fp_filters property (glob patterns like 'DIP*', 'SOIC*')
        and matches them against all available footprint names. Each suggestion includes
        physical dimensions (width_mm, height_mm) so you can make size-aware selections.

        Args:
            lib_id: Symbol identifier in 'Library:Symbol' format (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').

        Returns:
            JSON with fp_filters used and matching footprints list (with dimensions).
        """
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        ops = backend.get_library_ops()
        result = ops.suggest_footprints(lib_id)

        # Enrich each suggested footprint with physical dimensions
        enriched: list[dict] = []
        for fp in result.get("footprints", []):
            fp_id = fp.get("lib_id", "")
            entry = dict(fp)
            if fp_id:
                kicad_mod = _load_kicad_mod(fp_id)
                if kicad_mod is not None:
                    bounds = _parse_footprint_bounds(kicad_mod)
                    entry["width_mm"] = bounds["width_mm"]
                    entry["height_mm"] = bounds["height_mm"]
                    entry["area_mm2"] = round(bounds["width_mm"] * bounds["height_mm"], 4)
            enriched.append(entry)

        result["footprints"] = enriched
        change_log.record("suggest_footprints", {"lib_id": lib_id})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def estimate_board_size(
        footprint_ids: list[str],
        routing_overhead_pct: float = 20.0,
        edge_clearance_mm: float = 3.0,
        margin_pct: float = 25.0,
    ) -> str:
        """Estimate minimum board dimensions from a list of footprints.

        Calculates recommended board width and height before calling plan_project,
        so that board dimensions are driven by actual component footprints rather
        than guesswork. Call this with all key footprints before plan_project.

        Args:
            footprint_ids: List of footprint lib_ids to size for, e.g.
                ["RF_Module:ESP32-C3-WROOM-02", "Sensor_Humidity:SHT31-DIS"].
            routing_overhead_pct: Extra area for routing channels (default 20%).
            edge_clearance_mm: Board-edge-to-copper clearance added to each edge (default 3.0).
            margin_pct: Final dimensional margin applied after all other calculations (default 25%).

        Returns:
            JSON with recommended_width_mm, recommended_height_mm, area breakdown,
            per-component sizes, and any missing footprint IDs.
        """
        import math
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        per_component: list[dict] = []
        missing: list[str] = []
        component_area = 0.0

        for fp_id in footprint_ids:
            kicad_mod = _load_kicad_mod(fp_id)
            if kicad_mod is None:
                missing.append(fp_id)
                continue
            bounds = _parse_footprint_bounds(kicad_mod)
            w = bounds["width_mm"] if bounds["width_mm"] > 0 else 5.0
            h = bounds["height_mm"] if bounds["height_mm"] > 0 else 5.0
            area = round(w * h, 4)
            component_area += area
            per_component.append({
                "footprint": fp_id,
                "width_mm": round(w, 4),
                "height_mm": round(h, 4),
                "area_mm2": area,
            })

        if component_area == 0.0:
            # Nothing found — return a safe minimum
            component_area = 400.0  # 20×20 mm fallback

        routing_overhead = component_area * (routing_overhead_pct / 100.0)
        routed_area = component_area + routing_overhead

        # Landscape aspect ratio (width : height ≈ 1.4) using sqrt
        landscape_ratio = 1.4
        raw_width = math.sqrt(routed_area * landscape_ratio)
        raw_height = math.sqrt(routed_area / landscape_ratio)

        # Add edge clearance to both sides of each dimension
        raw_width += edge_clearance_mm * 2
        raw_height += edge_clearance_mm * 2

        def _ceil5(v: float) -> float:
            return math.ceil(v / 5.0) * 5.0

        # Round up to nearest 5 mm (standard fab panel snap)
        w5 = _ceil5(raw_width)
        h5 = _ceil5(raw_height)

        # Apply final dimensional margin
        final_w = _ceil5(w5 * (1.0 + margin_pct / 100.0))
        final_h = _ceil5(h5 * (1.0 + margin_pct / 100.0))

        edge_clearance_contrib = (edge_clearance_mm * 2) ** 2

        change_log.record("estimate_board_size", {"footprint_count": len(footprint_ids)})
        return json.dumps({
            "status": "success",
            "component_count": len(per_component),
            "missing_footprints": missing,
            "component_area_mm2": round(component_area, 2),
            "recommended_width_mm": final_w,
            "recommended_height_mm": final_h,
            "area_breakdown": {
                "component_area_mm2": round(component_area, 2),
                "routing_overhead_mm2": round(routing_overhead, 2),
                "edge_clearance_contribution_mm2": round(edge_clearance_contrib, 2),
                "margin_applied_pct": margin_pct,
            },
            "per_component": per_component,
        }, indent=2)

    @mcp.tool()
    def get_footprint_bounds(lib_id: str) -> str:
        """Get physical dimensions, courtyard bounds, and NPTH pad info for a footprint.

        Returns the courtyard rectangle (F.CrtYd) and pad geometry for any
        footprint before placing it on the board. Use this to compute
        non-overlapping placement positions and to check NPTH mounting hole
        clearances against fab design rules.

        Args:
            lib_id: Footprint identifier in 'Library:Footprint' format
                    (e.g. 'Resistor_SMD:R_0805_2012Metric').

        Returns:
            JSON with courtyard {xmin, ymin, xmax, ymax}, width_mm, height_mm,
            pads list, npth_pads list, and (when NPTH pads are present)
            min_npth_to_copper_mm — the minimum edge-to-edge clearance between
            any NPTH drill hole and any adjacent copper pad. Compare this against
            your fab's hole_clearance rule (JLCPCB: 0.25 mm) before selecting
            a connector with NPTH mounting holes.
        """
        from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds

        kicad_mod_text = _load_kicad_mod(lib_id)
        if kicad_mod_text is None:
            return json.dumps({
                "status": "error",
                "message": f"Footprint '{lib_id}' not found in system libraries.",
            }, indent=2)

        bounds = _parse_footprint_bounds(kicad_mod_text)
        change_log.record("get_footprint_bounds", {"lib_id": lib_id})
        return json.dumps({
            "status": "success",
            "lib_id": lib_id,
            **bounds,
        }, indent=2)
