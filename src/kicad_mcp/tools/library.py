"""Component library tools - 6 tools."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog

logger = get_logger("tools.library")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
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
        and matches them against all available footprint names.

        Args:
            lib_id: Symbol identifier in 'Library:Symbol' format (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').

        Returns:
            JSON with fp_filters used and matching footprints list.
        """
        ops = backend.get_library_ops()
        result = ops.suggest_footprints(lib_id)
        change_log.record("suggest_footprints", {"lib_id": lib_id})
        return json.dumps({"status": "success", **result}, indent=2)
