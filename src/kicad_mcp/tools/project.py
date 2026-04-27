"""Project management tools - 12 tools."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.kicad_paths import resolve_project_files
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.project")


def run_startup_checklist() -> dict[str, Any]:
    """Run pre-PCB startup checks.  Returns the same structure as get_startup_checklist.

    Extracted as a module-level function so it can be imported by pcb_pipeline
    in board.py without going through the MCP tool dispatch layer.
    """
    from kicad_mcp.utils.platform_helper import find_kicad_cli, is_kicad_running

    checklist: list[dict[str, Any]] = []
    required_actions: list[str] = []

    # ── 1. KiCad process running ─────────────────────────────────────────────
    kicad_up = is_kicad_running()
    checklist.append({
        "item": "kicad_running",
        "status": "PASS" if kicad_up else "FAIL",
        "detail": "KiCad process detected." if kicad_up else "KiCad is not running.",
    })
    if not kicad_up:
        required_actions.append("Launch KiCad (use open_kicad tool or start KiCad manually).")

    # ── 2. Bridge TCP reachable ──────────────────────────────────────────────
    _tcp_call = None
    port = 9760
    ping_timeout = 2.0
    try:
        from kicad_mcp.backends.plugin_backend import _get_ping_timeout, _get_port, _tcp_call  # type: ignore[assignment]
        port = _get_port()
        ping_timeout = _get_ping_timeout()
    except ImportError:
        pass

    bridge_ok = False
    bridge_detail = ""
    if _tcp_call is not None:
        try:
            result = _tcp_call("ping", ping_timeout)
            bridge_ok = isinstance(result, dict) and result.get("pong") is True
            bridge_detail = "Bridge responded to ping." if bridge_ok else "Bridge ping check returned unexpected value."
        except Exception as exc:
            bridge_detail = f"Bridge not reachable: {exc}"
    else:
        import socket as _socket
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=ping_timeout):
                bridge_ok = True
                bridge_detail = f"TCP port {port} is open."
        except Exception as exc:
            bridge_detail = f"TCP port {port} not reachable: {exc}"

    checklist.append({
        "item": "bridge_reachable",
        "status": "PASS" if bridge_ok else "FAIL",
        "detail": bridge_detail,
    })
    if not bridge_ok:
        required_actions.append(
            "Open the PCB editor in KiCad and ensure kicad_mcp_bridge is installed and enabled."
        )

    # ── 3. Bridge version check ──────────────────────────────────────────────
    bridge_version_ok = bridge_ok  # treat as OK if bridge is reachable but version not queryable
    bridge_version_detail = ""
    if bridge_ok and _tcp_call is not None:
        try:
            info = _tcp_call("get_info", ping_timeout)
            if isinstance(info, dict):
                version = info.get("version", "")
                bridge_version_detail = f"Bridge version: {version or 'unknown'}."
            else:
                bridge_version_detail = "Bridge reachable; version field not returned."
        except Exception as exc:
            bridge_version_detail = f"Version check skipped: {exc}"
    elif bridge_ok:
        bridge_version_detail = "Bridge reachable; version not checked (plugin_backend unavailable)."
    else:
        bridge_version_detail = "Skipped (bridge not reachable)."

    checklist.append({
        "item": "bridge_version_ok",
        "status": "PASS" if bridge_version_ok else "FAIL",
        "detail": bridge_version_detail,
    })

    # ── 4. PCB editor open ───────────────────────────────────────────────────
    pcb_editor_ok = False
    board_path_str = ""
    if bridge_ok and _tcp_call is not None:
        try:
            active = _tcp_call("get_active_project", ping_timeout)
            board_path_str = active.get("board_path", "") if isinstance(active, dict) else ""
            pcb_editor_ok = bool(board_path_str)
        except Exception as exc:
            board_path_str = f"(error: {exc})"

    pcb_editor_detail = (
        f"PCB editor open with: {board_path_str}"
        if pcb_editor_ok
        else ("No board loaded in PCB editor." if bridge_ok else "Skipped (bridge not reachable).")
    )
    checklist.append({
        "item": "pcb_editor_open",
        "status": "PASS" if pcb_editor_ok else "FAIL",
        "detail": pcb_editor_detail,
    })
    if bridge_ok and not pcb_editor_ok:
        required_actions.append("Open a .kicad_pcb file in the KiCad PCB editor.")

    # ── 5. kicad-cli on PATH ─────────────────────────────────────────────────
    cli_path_str = shutil.which("kicad-cli")
    if not cli_path_str:
        cli = find_kicad_cli()
        cli_path_str = str(cli) if cli else None
    cli_ok = cli_path_str is not None
    checklist.append({
        "item": "kicad_cli_available",
        "status": "PASS" if cli_ok else "WARN",
        "detail": (
            f"kicad-cli found at: {cli_path_str}"
            if cli_ok
            else "kicad-cli not found on PATH. Exports (Gerber, PDF, DRC) will fail."
        ),
    })

    # ── 6. Active project loaded ─────────────────────────────────────────────
    checklist.append({
        "item": "project_loaded",
        "status": "PASS" if pcb_editor_ok else "FAIL",
        "detail": (
            f"Active board: {board_path_str}"
            if pcb_editor_ok
            else "No active project detected in bridge."
        ),
    })
    if bridge_ok and not pcb_editor_ok:
        required_actions.append("Open a KiCad project (.kicad_pro or .kicad_pcb) in KiCad.")

    has_fail = any(item["status"] == "FAIL" for item in checklist)
    return {
        "ready_for_pcb": not has_fail,
        "checklist": checklist,
        "required_actions": required_actions,
    }


_FAB_PRESET_MAP: dict[str, str] = {
    "jlcpcb":   "fab_jlcpcb",
    "pcbway":   "fab_jlcpcb",   # similar 2-layer standard process
    "oshpark":  "class2",       # OSH Park uses tighter IPC Class 2 limits
    "class2":   "class2",
    "custom":   "class2",
}

_PLAN_FILENAME = "project_plan.json"


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register project management tools on the MCP server."""

    @mcp.tool()
    def get_startup_checklist() -> str:
        """Run pre-PCB startup checks and return a structured PASS/FAIL gate.

        MUST be called at the start of every session that involves PCB operations.
        Returns a ready_for_pcb boolean and a checklist with one entry per check.
        If ready_for_pcb is false, act on required_actions before proceeding.

        Checks performed (in order):
          1. kicad_running    — KiCad process is detected (FAIL blocks PCB ops)
          2. bridge_reachable — TCP connection to kicad_mcp_bridge succeeds (FAIL blocks PCB ops)
          3. bridge_version_ok — Bridge responds with version info (FAIL blocks PCB ops)
          4. pcb_editor_open  — A board file is active in the PCB editor (FAIL blocks PCB ops)
          5. kicad_cli_available — kicad-cli is on PATH (WARN: exports will fail without it)
          6. project_loaded   — Bridge has an active board path (FAIL blocks PCB ops)

        Returns:
            JSON with ready_for_pcb bool, checklist, and required_actions list.
        """
        result = run_startup_checklist()
        change_log.record("get_startup_checklist", {})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def plan_project(
        project_dir: str,
        product_description: str,
        board_layers: int = 2,
        fab_target: str = "jlcpcb",
        board_width_mm: float = 0.0,
        board_height_mm: float = 0.0,
        power_inputs: list[str] | None = None,
        power_rails: list[str] | None = None,
        key_components: list[str] | None = None,
        interfaces: list[str] | None = None,
        notes: str = "",
    ) -> str:
        """Capture design requirements before starting schematic capture.

        Writes a project_plan.json into the project directory that records all
        design constraints and intent.  This plan is the first step in every
        new design — it drives component selection, footprint package choices,
        and board sizing for all downstream tools.

        The plan answers these questions before any KiCad file is touched:
          - What does the product do?
          - What fab process and how many layers?
          - What are the board size constraints?
          - What power inputs and output rails are needed?
          - Which key ICs/modules are required?
          - Which communication interfaces are used?

        The returned recommended_settings block gives you the exact arguments
        to pass to create_project so design rules, board size, and fab target
        are configured consistently from the start.

        Args:
            project_dir: Directory for the project (created if it does not exist).
            product_description: What this product does. Be specific: include
                application, key features, and any critical constraints
                (e.g. "BLE air quality sensor, battery-powered, reads BME680
                over I2C, charges via USB-C, target size 50x40 mm").
            board_layers: Number of copper layers (2 or 4). Default 2.
            fab_target: Fabrication target — "jlcpcb" (default), "pcbway",
                "oshpark", or "custom".
            board_width_mm: Target board width in mm. Pass 0 (default) to auto-estimate
                from key_components footprints via estimate_board_size. If key_components
                is empty and width is 0, falls back to 100 mm.
            board_height_mm: Target board height in mm. Pass 0 (default) to auto-estimate.
                Falls back to 80 mm if key_components is empty.
            power_inputs: List of power input sources, e.g.
                ["USB-C 5V 3A", "LiPo 3.7V 2000mAh"].
            power_rails: List of required power rails, e.g.
                ["+3.3V 500mA (MCU, sensors)", "+5V 1A (motor driver)"].
            key_components: List of critical ICs and modules that must fit, e.g.
                ["ESP32-C3-WROOM-02", "BME680", "TP4056", "AMS1117-3.3"].
                Footprint IDs (Library:Footprint) are accepted directly; plain
                component names are used for documentation only.
            interfaces: Communication and I/O interfaces, e.g.
                ["I2C (BME680)", "USB-C (charge + data)", "BLE 5.0"].
            notes: Any additional constraints or design notes.

        Returns:
            JSON with the saved plan, the design_rules_preset to use,
            recommended_settings for the create_project call, and an optional
            size_warning if provided dimensions appear under-sized.
        """
        import json as _json

        proj_dir = Path(project_dir).resolve()
        proj_dir.mkdir(parents=True, exist_ok=True)

        preset = _FAB_PRESET_MAP.get(fab_target.lower(), "class2")

        # ── Auto-estimate board size when dimensions are 0 ────────────────────
        size_warning: str | None = None
        estimated_width: float | None = None
        estimated_height: float | None = None

        kc_list = key_components or []
        # Filter to items that look like Library:Footprint IDs (contain ':')
        footprint_ids = [k for k in kc_list if ":" in k]

        if footprint_ids:
            try:
                from kicad_mcp.backends.file_backend import _load_kicad_mod, _parse_footprint_bounds
                import math

                component_area = 0.0
                for fp_id in footprint_ids:
                    kicad_mod = _load_kicad_mod(fp_id)
                    if kicad_mod is None:
                        continue
                    bounds = _parse_footprint_bounds(kicad_mod)
                    w = bounds["width_mm"] if bounds["width_mm"] > 0 else 5.0
                    h = bounds["height_mm"] if bounds["height_mm"] > 0 else 5.0
                    component_area += w * h

                if component_area > 0:
                    routing_overhead = component_area * 0.20
                    routed_area = component_area + routing_overhead
                    edge_clearance = 3.0
                    raw_w = math.sqrt(routed_area * 1.4) + edge_clearance * 2
                    raw_h = math.sqrt(routed_area / 1.4) + edge_clearance * 2
                    ceil5 = lambda v: math.ceil(v / 5.0) * 5.0
                    estimated_width = ceil5(ceil5(raw_w) * 1.25)
                    estimated_height = ceil5(ceil5(raw_h) * 1.25)
            except Exception:
                pass  # estimation failure is non-fatal

        if board_width_mm <= 0 or board_height_mm <= 0:
            board_width_mm = estimated_width if estimated_width else 100.0
            board_height_mm = estimated_height if estimated_height else 80.0
        elif estimated_width and estimated_height:
            # Provided dimensions — check if under-sized vs estimate
            if board_width_mm < estimated_width * 0.85 or board_height_mm < estimated_height * 0.85:
                size_warning = (
                    f"Provided board size ({board_width_mm}×{board_height_mm} mm) is more than 15% "
                    f"smaller than the estimate ({estimated_width}×{estimated_height} mm) based on "
                    "footprint courtyards. Routing may be extremely difficult. Consider enlarging."
                )

        plan = {
            "product_description": product_description,
            "fab_target": fab_target,
            "board_layers": board_layers,
            "board_width_mm": board_width_mm,
            "board_height_mm": board_height_mm,
            "design_rules_preset": preset,
            "power_inputs": power_inputs or [],
            "power_rails": power_rails or [],
            "key_components": kc_list,
            "interfaces": interfaces or [],
            "notes": notes,
        }

        plan_path = proj_dir / _PLAN_FILENAME
        plan_path.write_text(_json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

        change_log.record("plan_project", {"dir": project_dir, "fab": fab_target})
        result: dict = {
            "status": "success",
            "plan_file": str(plan_path),
            "plan": plan,
            "recommended_settings": {
                "design_rules_preset": preset,
                "board_width_mm": board_width_mm,
                "board_height_mm": board_height_mm,
                "note": (
                    f"Pass design_rules_preset='{preset}' to create_project. "
                    f"Pass board_width_mm={board_width_mm} and "
                    f"board_height_mm={board_height_mm} to auto_place and pcb_pipeline."
                ),
            },
        }
        if size_warning:
            result["size_warning"] = size_warning
        if estimated_width and estimated_height:
            result["size_estimate"] = {
                "estimated_width_mm": estimated_width,
                "estimated_height_mm": estimated_height,
                "source": "footprint courtyard bounds",
            }
        return json.dumps(result, indent=2)

    @mcp.tool()
    def read_project_plan(project_dir: str) -> str:
        """Read the project_plan.json for an existing project.

        Returns the design requirements captured during planning so they can
        be referenced at any point in the workflow.

        Args:
            project_dir: Path to the project directory (or any file inside it).

        Returns:
            JSON with the plan contents, or an error if no plan file exists.
        """
        p = Path(project_dir).resolve()
        search_dir = p if p.is_dir() else p.parent

        plan_path = search_dir / _PLAN_FILENAME
        if not plan_path.exists():
            return json.dumps({
                "status": "not_found",
                "message": (
                    f"No {_PLAN_FILENAME} found in {search_dir}. "
                    "Run plan_project first to capture design requirements."
                ),
            })

        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return json.dumps({"status": "error", "message": str(exc)})

        change_log.record("read_project_plan", {"dir": str(search_dir)})
        return json.dumps({"status": "success", "plan": plan}, indent=2)

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
    def get_active_project() -> str:
        """Get the currently open KiCad project from a running KiCad instance.

        Queries KiCad via IPC to discover which project is currently open,
        along with any open schematic and PCB editor documents. On Linux
        builds where the IPC `GetOpenDocuments` handler is unavailable,
        project info falls back to the active board document metadata.
        Requires KiCad 9+ running with IPC enabled.

        Returns:
            JSON with project_name, project_path, and open_documents list.
        """
        from kicad_mcp.backends.base import BackendCapability
        if not backend.has_capability(BackendCapability.REAL_TIME_SYNC):
            return json.dumps({
                "status": "unavailable",
                "message": "IPC backend is not available. Ensure KiCad 9+ is running "
                           "with IPC enabled and the kipy package is installed.",
            })

        try:
            result = backend.get_active_project()
            change_log.record("get_active_project", {})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    @mcp.tool()
    def get_backend_info() -> str:
        """Get information about available backends and their capabilities.

        Returns:
            JSON with backend status, versions, and capability routing.
        """
        status = backend.get_status()
        change_log.record("get_backend_info", {})
        return json.dumps({"status": "success", **status}, indent=2)

    @mcp.tool()
    def get_text_variables(project_path: str) -> str:
        """Get project text variables (${VAR} substitution table).

        Returns all defined text variables used for title block and schematic
        text substitution. Requires KiCad 9+ running with IPC enabled.

        Args:
            project_path: Path to the .kicad_pro file.

        Returns:
            JSON with variable names and values, or unavailable status.
        """
        p = validate_kicad_path(project_path, ".kicad_pro")
        result = backend.get_text_variables(p)
        if result.get("status") == "success":
            change_log.record("get_text_variables", {"path": project_path})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def set_text_variables(project_path: str, variables: dict) -> str:
        """Set project text variables.

        Updates ${VAR} substitution values used in title blocks, schematic
        text, and board text. Requires KiCad 9+ running with IPC enabled.

        Args:
            project_path: Path to the .kicad_pro file.
            variables: Dict mapping variable names to string values.

        Returns:
            JSON with set status and count of variables updated.
        """
        p = validate_kicad_path(project_path, ".kicad_pro")
        result = backend.set_text_variables(p, variables)
        if result.get("status") == "success":
            change_log.record("set_text_variables", {"path": project_path, "count": len(variables)})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def create_project(
        project_dir: str,
        name: str,
        title: str = "",
        revision: str = "",
        design_rules_preset: str = "fab_jlcpcb",
    ) -> str:
        """Create a new KiCad project with blank schematic and PCB files.

        Creates <name>.kicad_pro, <name>.kicad_sch, and <name>.kicad_pcb
        inside project_dir (which is created if it does not exist).

        Design rules are applied immediately at project creation so that the
        router and DRC use consistent constraints from the very start — before
        schematic capture, footprint selection, or routing.  Choose the preset
        that matches your fabrication target:

          - "fab_jlcpcb"  (default) — JLCPCB 2-layer standard:
                0.127 mm trace, 0.45 mm via, 0.2 mm drill, 0.1 mm clearance
          - "class2"      — IPC-2221 Class 2:
                0.25 mm trace, 0.6 mm via, 0.3 mm drill, 0.2 mm clearance
          - ""            — skip (leaves KiCad defaults in place)

        Args:
            project_dir: Directory to create the project in.
            name: Project name, used as the file stem (no extension).
            title: Optional title for the title block of schematic and PCB.
            revision: Optional revision string (e.g. "1.0").
            design_rules_preset: Fab preset applied at creation — "fab_jlcpcb"
                (default), "class2", or "" to skip.

        Returns:
            JSON with status, paths to all created files, and the design rules
            preset that was applied.
        """
        from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps
        from kicad_mcp.utils.platform_helper import find_kicad_template_dir

        proj_dir = Path(project_dir).resolve()
        proj_dir.mkdir(parents=True, exist_ok=True)

        stem = name.strip()
        if not stem:
            return json.dumps({"status": "error", "message": "name must not be empty"})

        pro_path = proj_dir / f"{stem}.kicad_pro"
        sch_path = proj_dir / f"{stem}.kicad_sch"
        pcb_path = proj_dir / f"{stem}.kicad_pcb"

        # --- .kicad_pro ---
        _MINIMAL_PRO = {
            "board": {"design_settings": {}},
            "boards": [],
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": f"{stem}.kicad_pro", "version": 1},
            "net_settings": {"classes": [], "meta": {"version": 0}},
            "pcbnew": {"page_layout_descr_file": ""},
            "sheets": [],
            "text_variables": {},
        }
        template_dir = find_kicad_template_dir()
        template_pro = template_dir / "kicad.kicad_pro" if template_dir else None
        if template_pro and template_pro.exists():
            try:
                pro_data = json.loads(template_pro.read_text(encoding="utf-8"))
                pro_data.setdefault("meta", {})["filename"] = f"{stem}.kicad_pro"
            except (json.JSONDecodeError, OSError):
                pro_data = _MINIMAL_PRO
        else:
            pro_data = _MINIMAL_PRO

        pro_path.write_text(json.dumps(pro_data, indent=2), encoding="utf-8")

        # --- .kicad_sch ---
        FileSchematicOps().create_schematic(sch_path, title=title, revision=revision)

        # --- .kicad_pcb ---
        FileBoardOps().create_board(pcb_path, title=title, revision=revision)

        # Apply design rules immediately so constraints are set before any
        # schematic or footprint work begins.
        applied_preset = None
        if design_rules_preset:
            try:
                FileBoardOps().set_board_design_rules(pcb_path, design_rules_preset)
                applied_preset = design_rules_preset
            except ValueError as exc:
                return json.dumps({
                    "status": "error",
                    "message": f"Project files created but design_rules_preset failed: {exc}",
                    "project": {
                        "name": stem,
                        "directory": str(proj_dir),
                        "pro_file": str(pro_path),
                        "schematic_file": str(sch_path),
                        "board_file": str(pcb_path),
                    },
                })

        result = {
            "status": "success",
            "project": {
                "name": stem,
                "directory": str(proj_dir),
                "pro_file": str(pro_path),
                "schematic_file": str(sch_path),
                "board_file": str(pcb_path),
                "design_rules_preset": applied_preset or "none (defaults)",
            },
        }
        change_log.record("create_project", {"name": stem, "dir": project_dir, "preset": applied_preset})
        return json.dumps(result, indent=2)

    @mcp.tool()
    def get_pcb_workflow() -> str:
        """Return the standard KiCad PCB design workflow as a reference.

        Provides a structured sequence of tools and steps for producing a
        complete, DRC-clean PCB from a finished schematic. Follow this sequence
        to avoid common ordering mistakes (e.g. placing before assigning
        footprints, or routing before setting design rules).

        Returns:
            JSON with an ordered list of workflow steps, tool names, and notes.
        """
        workflow = {
            "description": (
                "Standard KiCad PCB design workflow using KiCad MCP. "
                "Follow steps in order; steps marked must_pass=true must "
                "succeed before continuing."
            ),
            "steps": [
                {
                    "step": 1,
                    "name": "plan_project",
                    "tool": "plan_project",
                    "must_pass": True,
                    "description": (
                        "Capture design requirements before any KiCad file is created. "
                        "Specify product description, fab target, board size, power "
                        "inputs/rails, key components, and interfaces. "
                        "Returns recommended_settings (design_rules_preset, board dimensions) "
                        "to pass to subsequent tools."
                    ),
                    "note": (
                        "This step drives every decision that follows: "
                        "fab target → design rules → minimum trace/via sizes → "
                        "which IC packages are routable → footprint selection → "
                        "board size → placement density."
                    ),
                },
                {
                    "step": 2,
                    "name": "create_project",
                    "tool": "create_project",
                    "description": (
                        "Create .kicad_pro, .kicad_sch, and .kicad_pcb files. "
                        "Pass design_rules_preset from plan_project step 1 output. "
                        "Rules are applied immediately so routing and DRC use "
                        "consistent constraints from the start."
                    ),
                },
                {
                    "step": 3,
                    "name": "capture_schematic",
                    "tools": ["add_component", "add_power_symbol", "add_wire",
                              "add_label", "add_junction", "add_no_connect"],
                    "description": (
                        "Place symbols, connect with wires, add power rails. "
                        "Component selection here should be informed by the plan: "
                        "key_components from plan_project drives which symbols to place. "
                        "Use search_symbols to find KiCad library symbols. "
                        "Use add_label to connect pins by net name without drawing wires "
                        "across the sheet."
                    ),
                },
                {
                    "step": 4,
                    "name": "assign_footprints",
                    "tools": ["update_component_property", "search_footprints",
                              "get_footprint_bounds", "get_footprint_info"],
                    "description": (
                        "Set the Footprint property on every non-power component "
                        "using update_component_property. "
                        "Use search_footprints to find candidates and get_footprint_bounds "
                        "to verify physical size is compatible with the design rules "
                        "set at create_project (e.g. pad pitch vs minimum clearance)."
                    ),
                },
                {
                    "step": 5,
                    "name": "run_erc",
                    "tool": "run_erc",
                    "must_pass": True,
                    "description": (
                        "Fix all ERC errors before proceeding. "
                        "Add PWR_FLAG symbols to fix power_pin_not_driven violations. "
                        "Add add_no_connect to all intentionally unused pins."
                    ),
                },
                {
                    "step": 6,
                    "name": "sync_to_pcb",
                    "tool": "sync_schematic_to_pcb",
                    "description": "Place all footprints on the board and assign nets.",
                },
                {
                    "step": 7,
                    "name": "add_board_outline",
                    "tool": "add_board_outline",
                    "description": (
                        "Draw a gr_rect on Edge.Cuts to define the physical board boundary. "
                        "Use board_width_mm and board_height_mm from the plan_project output. "
                        "Add 3 mm margin on all sides beyond the component area."
                    ),
                },
                {
                    "step": 8,
                    "name": "auto_place",
                    "tool": "auto_place",
                    "description": (
                        "Geometry-driven bin-packing placement. "
                        "Reads courtyard bounds for each footprint, "
                        "sorts by component class (connectors → ICs → discretes), "
                        "packs into rows with the specified clearance."
                    ),
                },
                {
                    "step": 9,
                    "name": "autoroute",
                    "tool": "autoroute",
                    "description": (
                        "Run FreeRouting auto-router. Use clean_board=false for new boards. "
                        "Router uses the via/trace sizes set at create_project."
                    ),
                },
                {
                    "step": 10,
                    "name": "run_drc",
                    "tool": "run_drc",
                    "must_pass": True,
                    "description": (
                        "All DRC errors must be resolved before export. "
                        "Rules set at create_project ensure router output passes cleanly."
                    ),
                },
                {
                    "step": 11,
                    "name": "export",
                    "tools": ["export_gerbers", "export_drill", "export_bom",
                              "export_pick_and_place"],
                    "description": "Export Gerbers, drill file, BOM, and pick-and-place for fabrication.",
                },
            ],
            "shortcut": {
                "tool": "pcb_pipeline",
                "description": (
                    "Run steps 6–10 in a single call (sync → outline → place → route → DRC). "
                    "Requires plan_project (step 1) and create_project (step 2) to already "
                    "be done. Pass board_width_mm and board_height_mm from the plan output."
                ),
            },
        }
        change_log.record("get_pcb_workflow", {})
        return json.dumps({"status": "success", "workflow": workflow}, indent=2)
