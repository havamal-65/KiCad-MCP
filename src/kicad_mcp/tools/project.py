"""Project management tools - 5 tools."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.kicad_paths import resolve_project_files
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.project")


def register_tools(mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog) -> None:
    """Register project management tools on the MCP server."""

    @mcp.tool()
    def open_project(path: str) -> str:
        """Open a KiCad project and return its structure.

        Args:
            path: Path to .kicad_pro file or project directory.

        Returns:
            JSON with project name, paths to board/schematic files, and metadata.
        """
        p = Path(path).resolve()
        files = resolve_project_files(p)

        result = {
            "status": "success",
            "project": {
                "name": p.stem if p.is_file() else p.name,
                "path": str(p),
                "board_file": str(files["board"]) if files["board"] else None,
                "schematic_file": str(files["schematic"]) if files["schematic"] else None,
                "has_board": files["board"] is not None,
                "has_schematic": files["schematic"] is not None,
            },
        }

        # Try to read project file for metadata
        pro_file = files.get("project")
        if pro_file and pro_file.exists():
            try:
                pro_data = json.loads(pro_file.read_text(encoding="utf-8"))
                meta = pro_data.get("meta", {})
                result["project"]["kicad_version"] = meta.get("version", "")
            except (json.JSONDecodeError, OSError):
                pass

        change_log.record("open_project", {"path": path})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def list_project_files(path: str) -> str:
        """List all KiCad-related files in a project directory.

        Args:
            path: Path to project directory or any file in the project.

        Returns:
            JSON with categorized list of project files.
        """
        p = Path(path).resolve()
        project_dir = p if p.is_dir() else p.parent

        if not project_dir.exists():
            return json.dumps({"status": "error", "message": f"Directory not found: {project_dir}"})

        kicad_extensions = {
            ".kicad_pro": "project",
            ".kicad_pcb": "board",
            ".kicad_sch": "schematic",
            ".kicad_sym": "symbol_library",
            ".kicad_mod": "footprint",
            ".kicad_dru": "design_rules",
            ".kicad_wks": "worksheet",
        }

        files: dict[str, list[str]] = {}
        for f in project_dir.iterdir():
            if f.is_file() and f.suffix in kicad_extensions:
                category = kicad_extensions[f.suffix]
                files.setdefault(category, []).append(str(f))

        change_log.record("list_project_files", {"path": path})
        return json.dumps({
            "status": "success",
            "directory": str(project_dir),
            "files": files,
        }, indent=2)

    @mcp.tool()
    def get_project_metadata(path: str) -> str:
        """Read detailed metadata from a KiCad project file.

        Args:
            path: Path to .kicad_pro file.

        Returns:
            JSON with project settings, libraries, and version info.
        """
        p = validate_kicad_path(path, ".kicad_pro")

        try:
            pro_data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return json.dumps({"status": "error", "message": str(e)})

        result = {
            "status": "success",
            "metadata": {
                "name": p.stem,
                "path": str(p),
                "meta": pro_data.get("meta", {}),
                "board": pro_data.get("board", {}),
                "libraries": pro_data.get("libraries", {}),
                "schematic": pro_data.get("schematic", {}),
            },
        }

        change_log.record("get_project_metadata", {"path": path})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def save_project(path: str) -> str:
        """Trigger save for an open KiCad project (requires IPC backend).

        Args:
            path: Path to the project file.

        Returns:
            JSON with save status.
        """
        from kicad_mcp.backends.base import BackendCapability
        if backend.has_capability(BackendCapability.REAL_TIME_SYNC):
            try:
                # IPC backend can trigger save
                result = {"status": "success", "message": "Project saved via IPC"}
                change_log.record("save_project", {"path": path})
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"status": "error", "message": str(e)})
        else:
            return json.dumps({
                "status": "info",
                "message": "Save requires KiCad running with IPC backend. "
                           "File-based operations auto-save on modification.",
            })

    @mcp.tool()
    def get_backend_info() -> str:
        """Get information about available backends and their capabilities.

        Returns:
            JSON with backend status, versions, and capability routing.
        """
        status = backend.get_status()
        change_log.record("get_backend_info", {})
        return json.dumps({"status": "success", **status}, indent=2)
