"""Auto-routing tools - 5 tools for PCB trace routing via FreeRouting."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

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


_HELPER_SCRIPT = Path(__file__).parent.parent / "utils" / "pcbnew_helper.py"


def _find_kicad_python() -> Path | None:
    """Find KiCad's bundled Python interpreter on the current platform.

    On macOS and Windows, KiCad ships its own Python that includes the
    ``pcbnew`` module.  On Linux, ``pcbnew`` is typically importable
    system-wide via ``/usr/bin/python3``.

    Returns:
        Path to the interpreter, or ``None`` if not found.
    """
    from kicad_mcp.utils.platform_helper import get_platform

    platform = get_platform()

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            candidate = Path(program_files) / "KiCad" / version / "bin" / "python.exe"
            if candidate.exists():
                return candidate
    elif platform == "macos":
        candidate = Path(
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/Current/bin/python3"
        )
        if candidate.exists():
            return candidate
    else:
        # On Linux, pcbnew is typically importable from the system Python.
        candidate = Path("/usr/bin/python3")
        if candidate.exists():
            return candidate

    return None


def _run_pcbnew_helper(
    command: str,
    args: list[str],
    timeout: int = 60,
) -> dict[str, Any]:
    """Invoke a pcbnew operation via the packaged helper script.

    Uses KiCad's bundled Python interpreter so that the ``pcbnew`` module
    is available even when it cannot be imported into the MCP server's own
    Python environment (the common case on macOS and Windows).

    Arguments are passed on the command line — no code generation — and the
    result is returned as a parsed JSON dict from the helper's stdout.

    Args:
        command: Subcommand name (e.g. ``"export_dsn"``).
        args: Positional string arguments for the subcommand.
        timeout: Subprocess timeout in seconds.

    Returns:
        Parsed JSON dict from the helper.  Always contains ``"ok": bool``.
        On failure, also contains ``"error": str``.
    """
    from kicad_mcp.utils.platform_helper import get_platform

    kicad_python = _find_kicad_python()
    if kicad_python is None:
        return {"ok": False, "error": "KiCad Python interpreter not found"}

    env = os.environ.copy()
    if get_platform() == "windows":
        kicad_bin = kicad_python.parent
        env["PYTHONHOME"] = str(kicad_bin)
        env["PYTHONPATH"] = ";".join([
            str(kicad_bin.parent / "lib" / "python3" / "dist-packages"),
            str(kicad_bin / "Lib" / "site-packages"),
            str(kicad_bin / "Lib"),
        ])

    cmd = [str(kicad_python), "-S", str(_HELPER_SCRIPT), command, *args]
    logger.debug("Running pcbnew helper: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"pcbnew helper timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": f"Failed to run KiCad Python: {exc}"}

    output = result.stdout.strip()
    if not output:
        error_detail = result.stderr.strip() or "no output"
        return {"ok": False, "error": f"pcbnew helper produced no output: {error_detail}"}

    try:
        return json.loads(output.splitlines()[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": f"pcbnew helper returned non-JSON output: {output[:200]}"}


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
        p = validate_kicad_path(path, ".kicad_pcb")
        dsn_path = Path(output) if output else p.parent / "freerouting.dsn"
        dsn_path = dsn_path.resolve()

        # Ensure output directory exists
        dsn_path.parent.mkdir(parents=True, exist_ok=True)

        pcbnew = _get_pcbnew()
        if pcbnew is not None:
            # Direct pcbnew API
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
            # pcbnew not importable in this Python environment —
            # delegate to the helper script running under KiCad's own Python.
            helper_result = _run_pcbnew_helper(
                "export_dsn", [str(p), str(dsn_path)]
            )
            if not helper_result.get("ok"):
                return json.dumps({
                    "status": "error",
                    "message": f"DSN export failed: {helper_result.get('error', 'unknown error')}",
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
        p = validate_kicad_path(path, ".kicad_pcb")
        ses = Path(ses_path).resolve()
        if not ses.exists():
            return json.dumps({
                "status": "error",
                "message": f"SES file not found: {ses}",
            })

        backup = create_backup(p)

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
            # pcbnew not importable in this Python environment —
            # delegate to the helper script running under KiCad's own Python.
            helper_result = _run_pcbnew_helper(
                "import_ses", [str(p), str(ses)]
            )
            if not helper_result.get("ok"):
                return json.dumps({
                    "status": "error",
                    "message": f"SES import failed: {helper_result.get('error', 'unknown error')}",
                })
            tracks_before = helper_result.get("tracks_before", 0)
            tracks_after = helper_result.get("tracks_after", 0)

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

        response = {
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
        p = validate_kicad_path(path, ".kicad_pcb")
        backup = create_backup(p)

        pcbnew = _get_pcbnew()
        if pcbnew is not None:
            try:
                board = pcbnew.LoadBoard(str(p))
                keepouts_removed = 0
                tracks_removed = 0

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
            # pcbnew not importable in this Python environment —
            # delegate to the helper script running under KiCad's own Python.
            helper_result = _run_pcbnew_helper(
                "clean_board",
                [str(p), str(remove_keepouts), str(remove_unassigned_tracks)],
            )
            if not helper_result.get("ok"):
                return json.dumps({
                    "status": "error",
                    "message": f"Board cleanup failed: {helper_result.get('error', 'unknown error')}",
                })
            keepouts_removed = helper_result.get("keepouts_removed", 0)
            tracks_removed = helper_result.get("tracks_removed", 0)

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
            result_json = clean_board_for_routing(path)
            result = json.loads(result_json)
            report["steps"].append({"step": "clean_board", **result})
            if result["status"] != "success":
                report["status"] = "error"
                report["message"] = f"Board cleanup failed: {result.get('message', '')}"
                return json.dumps(report, indent=2)

        # Step 2: Export DSN
        result_json = export_dsn(path, str(dsn))
        result = json.loads(result_json)
        report["steps"].append({"step": "export_dsn", **result})
        if result["status"] != "success":
            report["status"] = "error"
            report["message"] = f"DSN export failed: {result.get('message', '')}"
            return json.dumps(report, indent=2)

        # Step 3: Run FreeRouting
        result_json = run_freerouter(
            str(dsn), str(ses), max_passes, freerouting_jar, java_path,
        )
        result = json.loads(result_json)
        report["steps"].append({"step": "run_freerouter", **result})
        if result["status"] != "success":
            report["status"] = "error"
            report["message"] = f"FreeRouting failed: {result.get('message', '')}"
            # Clean up DSN
            dsn.unlink(missing_ok=True)
            return json.dumps(report, indent=2)

        # Step 4: Import SES
        result_json = import_ses(path, str(ses))
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
