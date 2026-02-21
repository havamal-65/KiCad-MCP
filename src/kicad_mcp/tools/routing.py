"""Auto-routing tools - 5 tools for PCB trace routing via FreeRouting."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
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


def _get_pcbnew():
    """Try to import pcbnew module.

    Returns:
        The pcbnew module, or None if not available.
    """
    try:
        import pcbnew
        return pcbnew
    except ImportError:
        return None


def _run_pcbnew_script(script: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a Python script using KiCad's bundled Python interpreter.

    Falls back to subprocess execution when pcbnew is not importable
    in the current Python environment.

    Args:
        script: Python code to execute.
        timeout: Timeout in seconds.

    Returns:
        Tuple of (success, output_text).
    """
    from kicad_mcp.utils.platform_helper import get_platform

    platform = get_platform()
    kicad_python = None

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            candidate = Path(program_files) / "KiCad" / version / "bin" / "python.exe"
            if candidate.exists():
                kicad_python = candidate
                break
    elif platform == "macos":
        candidate = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                         "Python.framework/Versions/Current/bin/python3")
        if candidate.exists():
            kicad_python = candidate
    else:
        # On Linux, pcbnew is usually importable system-wide
        kicad_python = Path("/usr/bin/python3")

    if kicad_python is None or not kicad_python.exists():
        return False, "KiCad Python interpreter not found"

    env = os.environ.copy()
    if platform == "windows":
        kicad_bin = kicad_python.parent
        env["PYTHONHOME"] = str(kicad_bin)
        env["PYTHONPATH"] = ";".join([
            str(kicad_bin.parent / "lib" / "python3" / "dist-packages"),
            str(kicad_bin / "Lib" / "site-packages"),
            str(kicad_bin / "Lib"),
        ])

    try:
        result = subprocess.run(
            [str(kicad_python), "-S", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Script timed out"
    except OSError as e:
        return False, f"Failed to run KiCad Python: {e}"


# ---------------------------------------------------------------------------
# Implementation helpers — plain Python functions callable from autoroute
# ---------------------------------------------------------------------------

def _impl_export_dsn(
    path: str,
    output: str,
    config: KiCadMCPConfig,
    change_log: ChangeLog,
) -> str:
    """Export a PCB board to Specctra DSN format."""
    p = validate_kicad_path(path, ".kicad_pcb")
    dsn_path = Path(output) if output else p.parent / "freerouting.dsn"
    dsn_path = dsn_path.resolve()

    dsn_path.parent.mkdir(parents=True, exist_ok=True)

    pcbnew = _get_pcbnew()
    if pcbnew is not None:
        try:
            board = pcbnew.LoadBoard(str(p))
            ok = pcbnew.ExportSpecctraDSN(board, str(dsn_path))
            if not ok:
                return json.dumps({
                    "status": "error",
                    "message": "ExportSpecctraDSN returned False. "
                               "Check for duplicate reference designators.",
                })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"pcbnew DSN export failed: {e}",
            })
    else:
        script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(p)!r})
ok = pcbnew.ExportSpecctraDSN(board, {str(dsn_path)!r})
if not ok:
    print("EXPORT_FAILED")
    sys.exit(1)
print("EXPORT_OK")
"""
        ok, output_text = _run_pcbnew_script(script)
        if not ok or "EXPORT_FAILED" in output_text:
            return json.dumps({
                "status": "error",
                "message": f"DSN export failed: {output_text}",
            })

    # Clean Unicode characters that FreeRouting can't handle
    if dsn_path.exists():
        content = dsn_path.read_text(encoding="utf-8")
        cleaned = re.sub("[ΩµΦ°]", "", content)
        dsn_path.write_text(cleaned, encoding="utf-8")

    if not dsn_path.exists():
        return json.dumps({
            "status": "error",
            "message": "DSN file was not created",
        })

    size = dsn_path.stat().st_size
    change_log.record("export_dsn", {"path": path, "output": str(dsn_path)})
    return json.dumps({
        "status": "success",
        "dsn_path": str(dsn_path),
        "size_bytes": size,
        "message": f"DSN exported: {dsn_path.name} ({size} bytes)",
    }, indent=2)


def _impl_import_ses(
    path: str,
    ses_path: str,
    change_log: ChangeLog,
) -> str:
    """Import a routed Specctra SES session file into a PCB board."""
    p = validate_kicad_path(path, ".kicad_pcb")
    ses = Path(ses_path).resolve()
    if not ses.exists():
        return json.dumps({
            "status": "error",
            "message": f"SES file not found: {ses}",
        })

    backup = create_backup(p)
    tracks_before = 0
    tracks_after = 0

    pcbnew = _get_pcbnew()
    if pcbnew is not None:
        try:
            board = pcbnew.LoadBoard(str(p))
            tracks_before = len(board.GetTracks())
            ok = pcbnew.ImportSpecctraSES(board, str(ses))
            if not ok:
                return json.dumps({
                    "status": "error",
                    "message": "ImportSpecctraSES returned False",
                })
            tracks_after = len(board.GetTracks())
            pcbnew.SaveBoard(str(p), board)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"SES import failed: {e}",
            })
    else:
        script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(p)!r})
before = len(board.GetTracks())
ok = pcbnew.ImportSpecctraSES(board, {str(ses)!r})
if not ok:
    print("IMPORT_FAILED")
    sys.exit(1)
after = len(board.GetTracks())
pcbnew.SaveBoard({str(p)!r}, board)
print(f"TRACKS_BEFORE={{before}}")
print(f"TRACKS_AFTER={{after}}")
"""
        ok, output_text = _run_pcbnew_script(script)
        if not ok or "IMPORT_FAILED" in output_text:
            return json.dumps({
                "status": "error",
                "message": f"SES import failed: {output_text}",
            })
        for line in output_text.splitlines():
            if line.startswith("TRACKS_BEFORE="):
                tracks_before = int(line.split("=")[1])
            elif line.startswith("TRACKS_AFTER="):
                tracks_after = int(line.split("=")[1])

    new_tracks = tracks_after - tracks_before
    change_log.record(
        "import_ses",
        {"path": path, "ses_path": ses_path},
        file_modified=path,
        backup_path=str(backup) if backup else None,
    )
    return json.dumps({
        "status": "success",
        "tracks_before": tracks_before,
        "tracks_after": tracks_after,
        "new_tracks": new_tracks,
        "message": f"Imported {new_tracks} routed tracks",
    }, indent=2)


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

    cmd = [
        str(java),
        "-jar", str(jar),
        "-de", str(dsn),
        "-do", str(ses),
        "-mp", str(max_passes),
    ]

    logger.info("Running FreeRouting: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "error",
            "message": "FreeRouting timed out after 300 seconds",
        })
    except OSError as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to run FreeRouting: {e}",
        })

    combined_output = (result.stdout or "") + (result.stderr or "")

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
                "message": f"Board cleanup failed: {e}",
            })
    else:
        script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(p)!r})
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
        ok, output_text = _run_pcbnew_script(script)
        if not ok:
            return json.dumps({
                "status": "error",
                "message": f"Board cleanup failed: {output_text}",
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
# MCP tool registration
# ---------------------------------------------------------------------------

def register_tools(
    mcp: FastMCP,
    backend: CompositeBackend,
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

        Args:
            path: Path to .kicad_pcb file.
            output: Output DSN file path. Defaults to <board_dir>/freerouting.dsn.

        Returns:
            JSON with export result and output file path.
        """
        return _impl_export_dsn(path, output, config, change_log)

    @mcp.tool()
    def import_ses(
        path: str,
        ses_path: str,
    ) -> str:
        """Import a routed Specctra SES session file into a PCB board.

        Applies auto-routed traces from a FreeRouting session file back
        into the KiCad PCB. Creates a backup before importing.

        Args:
            path: Path to .kicad_pcb file.
            ses_path: Path to .ses session file from FreeRouting.

        Returns:
            JSON with import result and track count.
        """
        return _impl_import_ses(path, ses_path, change_log)

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
    def autoroute(
        path: str,
        freerouting_jar: str = "",
        java_path: str = "",
        max_passes: int = 100,
        clean_board: bool = True,
    ) -> str:
        """Run the full auto-routing pipeline on a PCB board.

        Complete workflow: clean board -> export DSN -> run FreeRouting -> import SES.
        Creates a backup of the board before any modifications.

        Args:
            path: Path to .kicad_pcb file.
            freerouting_jar: Path to freerouting JAR. Auto-detected if empty.
            java_path: Path to java executable. Auto-detected if empty.
            max_passes: Maximum routing passes (default 100).
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

        # Step 2: Export DSN
        result_json = _impl_export_dsn(path, str(dsn), config, change_log)
        result = json.loads(result_json)
        report["steps"].append({"step": "export_dsn", **result})
        if result["status"] != "success":
            report["status"] = "error"
            report["message"] = f"DSN export failed: {result.get('message', '')}"
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

        # Step 4: Import SES
        result_json = _impl_import_ses(path, str(ses), change_log)
        result = json.loads(result_json)
        report["steps"].append({"step": "import_ses", **result})
        if result["status"] != "success":
            report["status"] = "error"
            report["message"] = f"SES import failed: {result.get('message', '')}"
        else:
            report["message"] = (
                f"Auto-routing complete: {result.get('new_tracks', 0)} tracks routed"
            )

        # Clean up temp files
        dsn.unlink(missing_ok=True)
        ses.unlink(missing_ok=True)

        change_log.record("autoroute", {"path": path, "max_passes": max_passes})
        return json.dumps(report, indent=2)
