"""Auto-routing tools - 5 tools for PCB trace routing via FreeRouting."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.backends.subprocess_backend import (
    _BOARD_CLEAN_TIMEOUT_SECONDS,
    _BOARD_LOAD_FAILED_SENTINEL,
    _format_pcbnew_error,
    _get_pcbnew,
    _malformed_board_message,
    _run_pcbnew_script,
)
from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.platform_helper import (
    download_freerouting,
    find_freerouting_jar,
    find_java,
)
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.routing")


# ---------------------------------------------------------------------------
# Implementation helpers — plain Python functions callable from autoroute
# ---------------------------------------------------------------------------

def _impl_run_freerouter(
    dsn_path: str,
    output: str,
    max_passes: int,
    freerouting_jar: str,
    java_path: str,
    config: KiCadMCPConfig,
    change_log: ChangeLog,
) -> str:
    """Run FreeRouting auto-router on a Specctra DSN file."""
    import re

    dsn = Path(dsn_path).resolve()
    if not dsn.exists():
        return json.dumps({
            "status": "error",
            "message": f"DSN file not found: {dsn}",
        })

    ses = Path(output).resolve() if output else dsn.with_suffix(".ses")

    # Resolve Java path
    java = None
    if java_path:
        java = Path(java_path)
    elif config.java_path:
        java = config.java_path
    else:
        java = find_java()

    if java is None or not java.exists():
        return json.dumps({
            "status": "error",
            "message": "Java executable not found. Install Java 17+ or set "
                       "KICAD_MCP_JAVA_PATH environment variable.",
        })

    # Resolve FreeRouting JAR
    jar = None
    if freerouting_jar:
        jar = Path(freerouting_jar)
    elif config.freerouting_jar:
        jar = config.freerouting_jar
    else:
        jar = find_freerouting_jar()

    # Auto-download if not found
    if jar is None or not jar.exists():
        logger.info("FreeRouting JAR not found, downloading automatically...")
        jar = download_freerouting()

    if jar is None or not jar.exists():
        return json.dumps({
            "status": "error",
            "message": "FreeRouting JAR not found and auto-download failed. "
                       "Download manually from "
                       "https://github.com/freerouting/freerouting/releases "
                       "or set KICAD_MCP_FREEROUTING_JAR environment variable.",
        })

    # v2.x requires --gui.enabled=false for headless/batch mode;
    # v1.x runs headlessly when -de/-do are provided without this flag.
    jar_is_v2 = re.search(r"freerouting-2\.", jar.name) is not None
    cmd = [
        str(java),
        "-jar", str(jar),
    ]
    if jar_is_v2:
        cmd.append("--gui.enabled=false")
    cmd += [
        "-de", str(dsn),
        "-do", str(ses),
        "-mp", str(max_passes),
    ]

    logger.info("Running FreeRouting: %s", " ".join(cmd))

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to run FreeRouting: {e}",
        })

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=85)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return json.dumps({
            "status": "error",
            "message": (
                f"FreeRouting timed out after 85 seconds (max_passes={max_passes}). "
                "Try a lower max_passes value."
            ),
        })

    combined_output = (
        stdout_bytes.decode(errors="replace") + stderr_bytes.decode(errors="replace")
    )

    # Parse routing stats from output
    routing_time = None
    improvement = None
    for line in combined_output.splitlines():
        if "auto-routing was completed in" in line.lower():
            match = re.search(r"(\d+\.?\d*)\s*seconds", line, re.IGNORECASE)
            if match:
                routing_time = float(match.group(1))
        if "improved the design by" in line.lower():
            match = re.search(r"~?(\d+\.?\d*)%", line)
            if match:
                improvement = float(match.group(1))

    if not ses.exists():
        return json.dumps({
            "status": "error",
            "message": "FreeRouting did not produce a session file. "
                       f"Output: {combined_output[:1000]}",
        })

    change_log.record("run_freerouter", {
        "dsn_path": dsn_path,
        "ses_path": str(ses),
        "max_passes": max_passes,
    })

    response: dict = {
        "status": "success",
        "ses_path": str(ses),
        "ses_size_bytes": ses.stat().st_size,
        "message": "FreeRouting completed successfully",
    }
    if routing_time is not None:
        response["routing_time_seconds"] = routing_time
    if improvement is not None:
        response["improvement_percent"] = improvement

    return json.dumps(response, indent=2)


def _impl_clean_board_for_routing(
    path: str,
    remove_keepouts: bool,
    remove_unassigned_tracks: bool,
    change_log: ChangeLog,
) -> str:
    """Clean a PCB board in preparation for auto-routing."""
    p = validate_kicad_path(path, ".kicad_pcb")
    backup = create_backup(p)
    keepouts_removed = 0
    tracks_removed = 0

    pcbnew = _get_pcbnew()
    if pcbnew is not None:
        try:
            board = pcbnew.LoadBoard(str(p))
            if board is None:
                return json.dumps({
                    "status": "error",
                    "message": _malformed_board_message(p),
                })

            if remove_keepouts:
                zones_to_remove = []
                for zone in board.Zones():
                    if zone.GetIsRuleArea():
                        zones_to_remove.append(zone)
                for zone in zones_to_remove:
                    board.Remove(zone)
                keepouts_removed = len(zones_to_remove)

            if remove_unassigned_tracks:
                tracks_to_remove = []
                for track in board.GetTracks():
                    net = track.GetNet()
                    net_name = net.GetNetname() if net else ""
                    if not net_name:
                        tracks_to_remove.append(track)
                for track in tracks_to_remove:
                    board.Remove(track)
                tracks_removed = len(tracks_to_remove)

            pcbnew.SaveBoard(str(p), board)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": _format_pcbnew_error("Board cleanup failed", str(e), p),
            })
    else:
        script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(p)!r})
if board is None:
    print("{_BOARD_LOAD_FAILED_SENTINEL}")
    sys.exit(2)
keepouts = 0
tracks = 0
if {remove_keepouts!r}:
    zones = [z for z in board.Zones() if z.GetIsRuleArea()]
    for z in zones:
        board.Remove(z)
    keepouts = len(zones)
if {remove_unassigned_tracks!r}:
    bad = [t for t in board.GetTracks() if not (t.GetNet() and t.GetNet().GetNetname())]
    for t in bad:
        board.Remove(t)
    tracks = len(bad)
pcbnew.SaveBoard({str(p)!r}, board)
print(f"KEEPOUTS={{keepouts}}")
print(f"TRACKS={{tracks}}")
"""
        ok, output_text = _run_pcbnew_script(script, timeout=_BOARD_CLEAN_TIMEOUT_SECONDS)
        if not ok:
            return json.dumps({
                "status": "error",
                "message": _format_pcbnew_error("Board cleanup failed", output_text, p),
            })
        for line in output_text.splitlines():
            if line.startswith("KEEPOUTS="):
                keepouts_removed = int(line.split("=")[1])
            elif line.startswith("TRACKS="):
                tracks_removed = int(line.split("=")[1])

    change_log.record(
        "clean_board_for_routing",
        {"path": path},
        file_modified=path,
        backup_path=str(backup) if backup else None,
    )
    return json.dumps({
        "status": "success",
        "keepouts_removed": keepouts_removed,
        "tracks_removed": tracks_removed,
        "message": (f"Removed {keepouts_removed} keepout zones and "
                    f"{tracks_removed} unassigned tracks"),
    }, indent=2)


# ---------------------------------------------------------------------------
# NPTH keepout injection helpers
# ---------------------------------------------------------------------------

def _extract_npth_pads(pcb_path: Path) -> list[dict]:
    """Return absolute positions and drill sizes for all NPTH pads in a .kicad_pcb file.

    Parses each footprint block, rotates the pad's local offset by the footprint
    rotation, and adds the footprint origin to get the board-coordinate centre.
    Returns a list of {"x": float, "y": float, "drill_mm": float} dicts.
    """
    import math
    import re as _re

    content = pcb_path.read_text(encoding="utf-8")
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    fp_at_re  = _re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)')
    pad_re    = _re.compile(r'\(pad\s+"[^"]*"\s+np_thru_hole')
    drill_re  = _re.compile(r'\(drill\s+([-\d.]+)')
    pat_at_re = _re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)')

    results: list[dict] = []
    i = 0
    n = len(content)

    while i < n:
        if content[i:i+11] != "(footprint ":
            i += 1
            continue
        end = _walk_balanced_parens(content, i)
        if end is None:
            i += 1
            continue
        fp_block = content[i:end + 1]

        # Footprint origin and rotation (first (at ...) in the block)
        at_m = fp_at_re.search(fp_block)
        if not at_m:
            i = end + 1
            continue
        fp_x   = float(at_m.group(1))
        fp_y   = float(at_m.group(2))
        fp_rot = float(at_m.group(3)) if at_m.group(3) else 0.0

        # Find each NPTH pad inside this footprint
        for pad_m in pad_re.finditer(fp_block):
            pad_start = pad_m.start()
            pad_end   = _walk_balanced_parens(fp_block, pad_start)
            if pad_end is None:
                continue
            pad_block = fp_block[pad_start:pad_end + 1]

            drill_m = drill_re.search(pad_block)
            if not drill_m:
                continue
            drill_mm = float(drill_m.group(1))

            # Pad local offset (skip the first (at ...) which belongs to footprint)
            pad_ats = list(pat_at_re.finditer(pad_block))
            if not pad_ats:
                dx, dy = 0.0, 0.0
            else:
                first_at = pad_ats[0]
                dx = float(first_at.group(1))
                dy = float(first_at.group(2))

            # Rotate local offset by footprint rotation (KiCad: positive angle = CW)
            if fp_rot:
                angle_rad = math.radians(fp_rot)
                cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
                rdx =  cos_a * dx + sin_a * dy
                rdy = -sin_a * dx + cos_a * dy
            else:
                rdx, rdy = dx, dy

            results.append({
                "x":        round(fp_x + rdx, 6),
                "y":        round(fp_y + rdy, 6),
                "drill_mm": drill_mm,
            })

        i = end + 1

    return results


def _inject_npth_keepouts_into_dsn(
    dsn_path: Path,
    npth_pads: list[dict],
    expansion_mm: float = 0.22,
) -> int:
    """Append keepout circles for NPTH pads into the DSN (structure ...) block.

    Returns the number of keepout entries written (0 if structure block not found).
    FreeRouting respects these and will not route copper through the drill areas.
    """
    import re as _re

    content = dsn_path.read_text(encoding="utf-8")

    # Collect signal layer names from the DSN structure
    layer_re = _re.compile(r'\(layer\s+"?([^")\s]+)"?\s+\(type\s+signal\)')
    layers = layer_re.findall(content)
    if not layers:
        # Fall back: accept any named layer
        layers = _re.findall(r'\(layer\s+"?([^")\s]+)"?', content)
    if not layers:
        return 0

    keepout_lines: list[str] = []
    for pad in npth_pads:
        radius = round(pad["drill_mm"] / 2.0 + expansion_mm, 4)
        x = round(pad["x"], 4)
        y = round(pad["y"], 4)
        for layer in layers:
            keepout_lines.append(
                f'    (keepout "" (circle "{layer}" {radius} {x} {y}))'
            )

    if not keepout_lines:
        return 0

    injection = "\n" + "\n".join(keepout_lines) + "\n  "

    # Insert before the closing ) of the (structure ...) block
    struct_start = content.find("(structure")
    if struct_start == -1:
        return 0

    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
    struct_end = _walk_balanced_parens(content, struct_start)
    if struct_end is None:
        return 0

    content = content[:struct_end] + injection + content[struct_end:]
    dsn_path.write_text(content, encoding="utf-8")
    return len(keepout_lines)


def _read_hole_clearance_from_pro(pcb_path: Path, default: float = 0.22) -> float:
    """Read min_hole_clearance from the sibling .kicad_pro design_settings."""
    import json as _json

    pro_path = pcb_path.with_suffix(".kicad_pro")
    try:
        pro = _json.loads(pro_path.read_text(encoding="utf-8"))
        return float(
            pro.get("board", {})
               .get("design_settings", {})
               .get("rules", {})
               .get("min_hole_clearance", default)
        )
    except Exception:
        return default


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_tools(
    mcp: FastMCP,
    backend: BackendProtocol,
    change_log: ChangeLog,
    config: KiCadMCPConfig,
) -> None:
    """Register auto-routing tools on the MCP server."""

    @mcp.tool()
    def export_dsn(
        path: str,
        output: str = "",
    ) -> str:
        """Export a PCB board to Specctra DSN format for auto-routing.

        Exports the board to DSN format and cleans Unicode characters
        that FreeRouting cannot handle (Omega, mu, Phi, degree symbols).

        Routes to plugin bridge if active (reads live in-memory board),
        otherwise uses subprocess pcbnew.

        Args:
            path: Path to .kicad_pcb file.
            output: Output DSN file path. Defaults to <board_dir>/freerouting.dsn.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        dsn_path = Path(output) if output else p.parent / "freerouting.dsn"
        dsn_path = dsn_path.resolve()
        try:
            result = backend.export_dsn(p, dsn_path)
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)}, indent=2)

        # Inject NPTH keepout zones so FreeRouting avoids routing through drill holes
        try:
            npth_pads = _extract_npth_pads(p)
            if npth_pads:
                expansion = _read_hole_clearance_from_pro(p)
                n_keepouts = _inject_npth_keepouts_into_dsn(dsn_path, npth_pads, expansion)
                result["npth_keepouts_injected"] = n_keepouts
        except Exception as exc:
            logger.warning("NPTH keepout injection failed (non-fatal): %s", exc)

        change_log.record("export_dsn", {"path": path, "output": str(dsn_path)})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def import_ses(
        path: str,
        ses_path: str,
    ) -> str:
        """Import a routed Specctra SES session file into a PCB board.

        Applies auto-routed traces from a FreeRouting session file back
        into the KiCad PCB. Creates a backup before importing.

        Routes to plugin bridge if active (updates live in-memory board),
        otherwise uses subprocess pcbnew.

        Args:
            path: Path to .kicad_pcb file.
            ses_path: Path to .ses session file from FreeRouting.

        Returns:
            JSON with import result and track count.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ses = Path(ses_path).resolve()
        backup = create_backup(p)
        try:
            result = backend.import_ses(p, ses)
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)}, indent=2)
        change_log.record(
            "import_ses",
            {"path": path, "ses_path": ses_path},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        backend.reload_board(p)
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def run_freerouter(
        dsn_path: str,
        output: str = "",
        max_passes: int = 100,
        freerouting_jar: str = "",
        java_path: str = "",
    ) -> str:
        """Run FreeRouting auto-router on a Specctra DSN file.

        Executes the FreeRouting Java application to auto-route the PCB.
        Auto-detects Java and FreeRouting JAR if not provided.
        Downloads FreeRouting automatically if not found on the system.

        Args:
            dsn_path: Path to input .dsn file.
            output: Output .ses file path. Defaults to same directory as DSN.
            max_passes: Maximum routing passes (default 100).
            freerouting_jar: Path to freerouting JAR. Auto-detected if empty.
            java_path: Path to java executable. Auto-detected if empty.

        Returns:
            JSON with routing results including time and improvement stats.
        """
        return _impl_run_freerouter(
            dsn_path, output, max_passes, freerouting_jar, java_path,
            config, change_log,
        )

    @mcp.tool()
    def clean_board_for_routing(
        path: str,
        remove_keepouts: bool = True,
        remove_unassigned_tracks: bool = True,
    ) -> str:
        """Clean a PCB board in preparation for auto-routing.

        Removes keepout zones (rule areas) and tracks with no net assignment
        that would interfere with the auto-router. Creates a backup first.

        Args:
            path: Path to .kicad_pcb file.
            remove_keepouts: Remove all keepout/rule area zones (default true).
            remove_unassigned_tracks: Remove tracks with no net (default true).

        Returns:
            JSON with counts of removed items.
        """
        return _impl_clean_board_for_routing(
            path, remove_keepouts, remove_unassigned_tracks, change_log,
        )

    @mcp.tool()
    def clear_routes(path: str, backup: bool = True) -> str:
        """Remove all routed tracks and vias from a board, preserving component placement.

        Strips every (segment ...) and (via ...) block from the .kicad_pcb file
        so the board can be re-placed and re-routed without manual file surgery.
        If the plugin bridge is active, reloads the board so KiCad reflects the change.

        This is the correct tool to use when placement needs to be redone after routing
        has already started. Use clear_routes + auto_place instead of routing over bad
        placement.

        Args:
            path: Path to .kicad_pcb file.
            backup: Write a .clear_routes_backup.kicad_pcb file before modifying (default True).

        Returns:
            JSON with tracks_removed, vias_removed, and backup_path.
        """
        from kicad_mcp.backends.file_backend import FileBoardOps

        p = validate_kicad_path(path, ".kicad_pcb")
        result = FileBoardOps().clear_routes(p, backup=backup)

        # Reload the in-memory board so KiCad's PCB editor reflects the change
        if result.get("status") == "success":
            try:
                backend.reload_board(p)
            except Exception:
                pass  # reload is best-effort; file is already written

        change_log.record(
            "clear_routes",
            {
                "path": path,
                "tracks_removed": result.get("tracks_removed", 0),
                "vias_removed": result.get("vias_removed", 0),
            },
            file_modified=path,
            backup_path=result.get("backup_path"),
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    def autoroute(
        path: str,
        freerouting_jar: str = "",
        java_path: str = "",
        max_passes: int = 10,
        clean_board: bool = True,
    ) -> str:
        """Run the full auto-routing pipeline on a PCB board.

        Complete workflow: clean board -> export DSN -> run FreeRouting -> import SES.
        Creates a backup of the board before any modifications.

        Board outline and loadability are validated inside export_dsn — no separate
        preflight step is needed here.

        Args:
            path: Path to .kicad_pcb file.
            freerouting_jar: Path to freerouting JAR. Auto-detected if empty.
            java_path: Path to java executable. Auto-detected if empty.
            max_passes: Maximum routing passes (default 10). Increase for more
                complete routing at the cost of longer runtime.
            clean_board: Remove keepouts and bad tracks first (default true).

        Returns:
            JSON with comprehensive routing report.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        dsn = p.parent / "freerouting.dsn"
        ses = p.parent / "freerouting.ses"
        report: dict = {"status": "success", "steps": []}

        # Step 1: Clean board (optional)
        if clean_board:
            result_json = _impl_clean_board_for_routing(
                path, True, True, change_log,
            )
            result = json.loads(result_json)
            report["steps"].append({"step": "clean_board", **result})
            if result["status"] != "success":
                report["status"] = "error"
                report["message"] = f"Board cleanup failed: {result.get('message', '')}"
                return json.dumps(report, indent=2)

        # Step 2: Export DSN — routes to plugin bridge (reads live in-memory board)
        # or subprocess pcbnew, via BOARD_ROUTE capability.
        try:
            dsn_result = backend.export_dsn(p, dsn)
            # Inject NPTH keepout zones so FreeRouting avoids routing through drill holes
            try:
                npth_pads = _extract_npth_pads(p)
                if npth_pads:
                    expansion = _read_hole_clearance_from_pro(p)
                    n_keepouts = _inject_npth_keepouts_into_dsn(dsn, npth_pads, expansion)
                    dsn_result["npth_keepouts_injected"] = n_keepouts
            except Exception as npth_exc:
                logger.warning("NPTH keepout injection failed (non-fatal): %s", npth_exc)
            report["steps"].append({"step": "export_dsn", "status": "success", **dsn_result})
        except Exception as exc:
            report["status"] = "error"
            report["message"] = f"DSN export failed: {exc}"
            return json.dumps(report, indent=2)

        # Step 3: Run FreeRouting
        result_json = _impl_run_freerouter(
            str(dsn), str(ses), max_passes, freerouting_jar, java_path,
            config, change_log,
        )
        result = json.loads(result_json)
        report["steps"].append({"step": "run_freerouter", **result})
        if result["status"] != "success":
            report["status"] = "error"
            report["message"] = f"FreeRouting failed: {result.get('message', '')}"
            dsn.unlink(missing_ok=True)
            return json.dumps(report, indent=2)

        # Step 4: Import SES — routes to plugin bridge (updates live in-memory board + saves)
        # or subprocess pcbnew, via BOARD_ROUTE capability.
        try:
            ses_result = backend.import_ses(p, ses)
            report["steps"].append({"step": "import_ses", "status": "success", **ses_result})
            report["message"] = (
                f"Auto-routing complete: {ses_result.get('new_tracks', 0)} tracks routed"
            )
            result = {"status": "success", **ses_result}
        except Exception as exc:
            report["status"] = "error"
            report["message"] = f"SES import failed: {exc}"
            return json.dumps(report, indent=2)

        # Step 5: Post-route DRC (best-effort; flags shorts/errors without blocking)
        if result["status"] == "success":
            try:
                drc_ops = backend.get_drc_ops()
                drc_result = drc_ops.run_drc(p, None)
                drc_passed = drc_result.get("passed", False)
                error_count = drc_result.get("error_count", 0)
                report["steps"].append({
                    "step": "post_route_drc",
                    "status": "success",
                    "passed": drc_passed,
                    "error_count": error_count,
                    "warning_count": drc_result.get("warning_count", 0),
                })
                if not drc_passed:
                    report["status"] = "success_with_drc_errors"
                    report["message"] = (
                        f"Routed {result.get('new_tracks', 0)} tracks but DRC found "
                        f"{error_count} error(s) — inspect board before fabrication."
                    )
            except Exception as drc_exc:
                report["steps"].append({
                    "step": "post_route_drc",
                    "status": "unavailable",
                    "message": f"Post-route DRC skipped: {drc_exc}",
                })

        # Step 5: Post-route DRC (best-effort; flags shorts/errors without blocking)
        if result["status"] == "success":
            try:
                drc_ops = backend.get_drc_ops()
                drc_result = drc_ops.run_drc(p, None)
                drc_passed = drc_result.get("passed", False)
                error_count = drc_result.get("error_count", 0)
                report["steps"].append({
                    "step": "post_route_drc",
                    "status": "success",
                    "passed": drc_passed,
                    "error_count": error_count,
                    "warning_count": drc_result.get("warning_count", 0),
                })
                if not drc_passed:
                    report["status"] = "success_with_drc_errors"
                    report["message"] = (
                        f"Routed {result.get('new_tracks', 0)} tracks but DRC found "
                        f"{error_count} error(s) — inspect board before fabrication."
                    )
            except Exception as drc_exc:
                report["steps"].append({
                    "step": "post_route_drc",
                    "status": "unavailable",
                    "message": f"Post-route DRC skipped: {drc_exc}",
                })

        # Clean up temp files
        dsn.unlink(missing_ok=True)
        ses.unlink(missing_ok=True)

        change_log.record("autoroute", {"path": path, "max_passes": max_passes})
        return json.dumps(report, indent=2)
