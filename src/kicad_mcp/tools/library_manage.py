"""Library management tools - 9 tools for cloning, importing, and registering libraries."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup

logger = get_logger("tools.library_manage")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
    """Register library management tools on the MCP server."""

    @mcp.tool()
    def clone_library_repo(url: str, name: str, target_path: str = "") -> str:
        """Clone a remote KiCad library repository (shallow clone).

        Args:
            url: Git URL of the library repository to clone.
            name: Short name to register this source as (e.g. 'digikey', 'sparkfun').
            target_path: Optional local directory path. Defaults to ~/.kicad-mcp/external_libs/{name}.

        Returns:
            JSON with clone result including the local path.
        """
        ops = backend.get_library_manage_ops()
        result = ops.clone_library_repo(url, name, target_path or None)
        change_log.record("clone_library_repo", {"url": url, "name": name})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def register_library_source(path: str, name: str) -> str:
        """Register a local directory as a searchable KiCad library source.

        Args:
            path: Absolute path to a directory containing .kicad_sym files and/or .pretty directories.
            name: Short name to identify this source (e.g. 'project_libs', 'custom').

        Returns:
            JSON confirming registration.
        """
        ops = backend.get_library_manage_ops()
        result = ops.register_library_source(path, name)
        change_log.record("register_library_source", {"path": path, "name": name})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def list_library_sources() -> str:
        """List all registered external library sources.

        Returns:
            JSON array of registered sources with name, path, type, and URL.
        """
        ops = backend.get_library_manage_ops()
        sources = ops.list_library_sources()
        change_log.record("list_library_sources", {})
        return json.dumps({
            "status": "success",
            "count": len(sources),
            "sources": sources,
        }, indent=2)

    @mcp.tool()
    def unregister_library_source(name: str) -> str:
        """Remove a library source registration (files on disk are kept).

        Args:
            name: The name of the source to unregister.

        Returns:
            JSON confirming removal.
        """
        ops = backend.get_library_manage_ops()
        result = ops.unregister_library_source(name)
        change_log.record("unregister_library_source", {"name": name})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def search_library_sources(query: str, source_name: str = "") -> str:
        """Search for symbols and footprints across registered external library sources.

        Args:
            query: Text to match against symbol/footprint names (e.g. 'SCD41', 'ESP32').
            source_name: Optional source name to restrict search to a single source.

        Returns:
            JSON with matching symbols and footprints, including source paths.
        """
        ops = backend.get_library_manage_ops()
        result = ops.search_library_sources(query, source_name or None)
        change_log.record("search_library_sources", {"query": query, "source_name": source_name})
        return json.dumps({
            "status": "success",
            "query": query,
            "symbol_count": len(result["symbols"]),
            "footprint_count": len(result["footprints"]),
            **result,
        }, indent=2)

    @mcp.tool()
    def create_project_library(
        project_path: str, library_name: str, lib_type: str = "both",
    ) -> str:
        """Create an empty project-local KiCad library.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro) or project directory.
            library_name: Name for the new library (without extension).
            lib_type: Type of library to create: 'symbol', 'footprint', or 'both' (default).

        Returns:
            JSON listing the created files/directories.
        """
        ops = backend.get_library_manage_ops()
        result = ops.create_project_library(project_path, library_name, lib_type)
        change_log.record(
            "create_project_library",
            {"project_path": project_path, "library_name": library_name, "lib_type": lib_type},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def import_symbol(
        source_lib: str, symbol_name: str, target_lib_path: str,
    ) -> str:
        """Copy a symbol from one .kicad_sym library file to another.

        Args:
            source_lib: Path to the source .kicad_sym file.
            symbol_name: Exact name of the symbol to import (e.g. 'SCD41').
            target_lib_path: Path to the target .kicad_sym file to insert into.

        Returns:
            JSON confirming the import.
        """
        from pathlib import Path
        target = Path(target_lib_path)
        backup_path = create_backup(target) if target.exists() else None

        ops = backend.get_library_manage_ops()
        result = ops.import_symbol(source_lib, symbol_name, target_lib_path)
        change_log.record(
            "import_symbol",
            {"source_lib": source_lib, "symbol_name": symbol_name, "target_lib_path": target_lib_path},
            file_modified=target_lib_path,
            backup_path=str(backup_path) if backup_path else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def import_footprint(
        source_lib: str, footprint_name: str, target_lib_path: str,
    ) -> str:
        """Copy a .kicad_mod footprint file from one .pretty directory to another.

        Args:
            source_lib: Path to the source .pretty directory.
            footprint_name: Name of the footprint to import (without .kicad_mod extension).
            target_lib_path: Path to the target .pretty directory.

        Returns:
            JSON confirming the import.
        """
        ops = backend.get_library_manage_ops()
        result = ops.import_footprint(source_lib, footprint_name, target_lib_path)
        change_log.record(
            "import_footprint",
            {"source_lib": source_lib, "footprint_name": footprint_name, "target_lib_path": target_lib_path},
            file_modified=result.get("copied_file"),
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def register_project_library(
        project_path: str, library_name: str, library_path: str, lib_type: str,
    ) -> str:
        """Register a library in a project's sym-lib-table or fp-lib-table.

        Creates the table file if it doesn't exist. Uses ${KIPRJMOD} relative paths
        for project-local libraries.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro) or project directory.
            library_name: Name for the library entry in the table.
            library_path: Path to the .kicad_sym file or .pretty directory.
            lib_type: Either 'symbol' (for sym-lib-table) or 'footprint' (for fp-lib-table).

        Returns:
            JSON with the table file path and registered URI.
        """
        from pathlib import Path
        proj_dir = Path(project_path).parent if Path(project_path).suffix else Path(project_path)
        table_name = "sym-lib-table" if lib_type == "symbol" else "fp-lib-table"
        table_file = proj_dir / table_name
        backup_path = create_backup(table_file) if table_file.exists() else None

        ops = backend.get_library_manage_ops()
        result = ops.register_project_library(project_path, library_name, library_path, lib_type)
        change_log.record(
            "register_project_library",
            {"project_path": project_path, "library_name": library_name, "lib_type": lib_type},
            file_modified=str(table_file),
            backup_path=str(backup_path) if backup_path else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)
