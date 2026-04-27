"""Subprocess-based pcbnew backend for DSN/SES routing operations.

Runs pcbnew API calls in a KiCad-bundled Python subprocess when the plugin
bridge is not active.  Provides BOARD_ROUTE capability (export_dsn / import_ses)
only — all other BoardOps are intentionally unimplemented.

Helper functions and sentinels in this module are re-exported for use by
routing.py's _impl_clean_board_for_routing and _validate_board_preflight.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    KiCadBackend,
)
from kicad_mcp.logging_config import get_logger

logger = get_logger("backend.subprocess")


# ---------------------------------------------------------------------------
# Sentinel strings used in pcbnew subprocess output
# ---------------------------------------------------------------------------

_BOARD_LOAD_FAILED_SENTINEL = "BOARD_LOAD_FAILED"
_BOARD_OUTLINE_MISSING_SENTINEL = "BOARD_OUTLINE_MISSING"
_BOARD_OUTLINE_NO_SEGMENTS_SENTINEL = "BOARD_OUTLINE_NO_SEGMENTS"
_BOARD_OUTLINE_OPEN_SENTINEL = "BOARD_OUTLINE_OPEN"
_DSN_EXPORT_TIMEOUT_SECONDS = 900
_SES_IMPORT_TIMEOUT_SECONDS = 900
_BOARD_CLEAN_TIMEOUT_SECONDS = 300
_BOARD_PREFLIGHT_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# pcbnew import helpers
# ---------------------------------------------------------------------------

def _get_pcbnew():
    """Try to import pcbnew module.

    Returns:
        The pcbnew module, or None if not available.
    """
    from kicad_mcp.utils.platform_helper import add_kicad_to_sys_path
    add_kicad_to_sys_path()
    try:
        import pcbnew
        return pcbnew
    except ImportError:
        return None


def _get_kicad_python() -> Path | None:
    """Return the path to KiCad's bundled Python interpreter, or None if not found."""
    from kicad_mcp.utils.platform_helper import get_platform
    import shutil
    platform = get_platform()

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            candidate = Path(program_files) / "KiCad" / version / "bin" / "python.exe"
            if candidate.exists():
                return candidate
    elif platform == "macos":
        candidate = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                         "Python.framework/Versions/Current/bin/python3")
        if candidate.exists():
            return candidate
    else:  # linux
        # Snap installs bundle their own Python alongside KiCad.
        # Resolve `kicad` → e.g. /snap/kicad/current/usr/bin/kicad
        # and look for python3 in the same prefix directory.
        kicad_in_path = shutil.which("kicad")
        if kicad_in_path:
            real = Path(kicad_in_path).resolve()
            snap_python = real.parent / "python3"
            if snap_python.exists():
                return snap_python

        # System package install: the standard Python has pcbnew accessible
        # via /usr/lib/kicad/lib/python3/dist-packages (set up by _run_pcbnew_script).
        for candidate_path in ["/usr/bin/python3", "/usr/local/bin/python3"]:
            candidate = Path(candidate_path)
            if candidate.exists():
                return candidate

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
    kicad_python = _get_kicad_python()

    if kicad_python is None or not kicad_python.exists():
        return False, "KiCad Python interpreter not found"

    env = os.environ.copy()
    if platform == "windows":
        kicad_bin = kicad_python.parent
        env["PYTHONHOME"] = str(kicad_bin)
        env["PYTHONPATH"] = ";".join([
            str(kicad_bin / "Lib" / "site-packages"),
            str(kicad_bin / "Lib"),
        ])
        # Add kicad_bin to PATH so Windows can resolve DLLs when _pcbnew.pyd loads
        env["PATH"] = str(kicad_bin) + ";" + env.get("PATH", "")
        site_pkgs = str(kicad_bin / "Lib" / "site-packages")
        preamble = (
            f"import ctypes; ctypes.windll.kernel32.SetErrorMode(3)\n"
            f"import os, sys; "
            f"os.add_dll_directory({str(kicad_bin)!r}); "
            f"sys.path.insert(0, {site_pkgs!r})\n"
        )
        script = preamble + script
    elif platform == "macos":
        # Ensure KiCad's site-packages (containing pcbnew) are on PYTHONPATH.
        from kicad_mcp.utils.platform_helper import find_kicad_python_paths
        kicad_paths = [str(p) for p in find_kicad_python_paths()]
        if kicad_paths:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = ":".join(kicad_paths + ([existing] if existing else []))
    else:  # linux
        # For snap: Python lives in the snap prefix — pcbnew is already importable.
        # For system installs: add the KiCad dist-packages directory to PYTHONPATH.
        from kicad_mcp.utils.platform_helper import find_kicad_python_paths
        kicad_paths = [str(p) for p in find_kicad_python_paths()]
        if kicad_paths:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = ":".join(kicad_paths + ([existing] if existing else []))

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [str(kicad_python), "-S", "-u", "-c", script],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and not output.strip():
            output = (
                f"KiCad Python exited with code {result.returncode} "
                "and produced no diagnostic output."
            )
        return result.returncode == 0, output
    except subprocess.TimeoutExpired as e:
        partial_output = ""
        if isinstance(e.stdout, str):
            partial_output += e.stdout
        elif isinstance(e.stdout, bytes):
            partial_output += e.stdout.decode(errors="replace")
        if isinstance(e.stderr, str):
            partial_output += e.stderr
        elif isinstance(e.stderr, bytes):
            partial_output += e.stderr.decode(errors="replace")

        normalized = _normalize_error_text(partial_output)
        if normalized:
            return False, (
                f"Script timed out after {timeout} seconds. "
                f"Partial output: {normalized}"
            )
        return False, f"Script timed out after {timeout} seconds with no output."
    except OSError as e:
        return False, f"Failed to run KiCad Python: {e}"


# ---------------------------------------------------------------------------
# Error formatting helpers
# ---------------------------------------------------------------------------

def _normalize_error_text(text: str, max_chars: int = 1200) -> str:
    """Normalize multiline stderr into a compact single-line message."""
    compact = " ".join((text or "").split())
    if len(compact) > max_chars:
        return compact[:max_chars] + "..."
    return compact


def _malformed_board_message(board_path: Path, details: str = "") -> str:
    message = (
        f"Malformed board: KiCad could not load '{board_path}'. "
        "Ensure the .kicad_pcb file is valid before running autoroute."
    )
    normalized = _normalize_error_text(details)
    if normalized:
        message += f" Details: {normalized}"
    return message


def _board_outline_error_message(board_path: Path, details: str = "") -> str:
    message = (
        f"Board outline error: '{board_path}' must include a closed Edge.Cuts outline "
        "before running autoroute."
    )
    normalized = _normalize_error_text(details)
    if normalized:
        message += f" Details: {normalized}"
    return message


def _format_pcbnew_error(prefix: str, output_text: str, board_path: Path | None = None) -> str:
    """Build a stable error message from pcbnew subprocess output."""
    has_load_failed = _BOARD_LOAD_FAILED_SENTINEL in (output_text or "")
    cleaned = (output_text or "").replace(_BOARD_LOAD_FAILED_SENTINEL, "")
    normalized = _normalize_error_text(cleaned)

    if board_path is not None and (has_load_failed or not normalized):
        detail = normalized or "KiCad did not provide additional diagnostics."
        return _malformed_board_message(board_path, detail)

    if normalized:
        return f"{prefix}: {normalized}"
    return f"{prefix}: KiCad did not provide additional diagnostics."


# ---------------------------------------------------------------------------
# Edge.Cuts outline validation helpers
# ---------------------------------------------------------------------------

def _point_key(point: object) -> tuple[int, int] | None:
    x = getattr(point, "x", None)
    y = getattr(point, "y", None)
    if x is None or y is None:
        try:
            return int(point[0]), int(point[1])  # type: ignore[index]
        except Exception:
            return None
    return int(x), int(y)


def _is_closed_outline_item(item: object) -> bool:
    """Return True for closed geometric edge items (rectangles, polygons, circles)."""
    if hasattr(item, "IsClosed"):
        try:
            if bool(item.IsClosed()):
                return True
        except Exception:
            pass

    if hasattr(item, "GetShapeStr"):
        try:
            shape = str(item.GetShapeStr()).lower()
            if any(token in shape for token in ("rect", "polygon", "poly", "circle")):
                return True
        except Exception:
            pass

    return False


def _is_edge_cuts_item(item: object, board: object, edge_layer_id: int | None) -> bool:
    if hasattr(item, "GetLayerName"):
        try:
            if item.GetLayerName() == "Edge.Cuts":
                return True
        except Exception:
            pass

    if hasattr(item, "GetLayer"):
        try:
            layer = int(item.GetLayer())
            if edge_layer_id is not None:
                return layer == edge_layer_id
            if hasattr(board, "GetLayerName"):
                return board.GetLayerName(layer) == "Edge.Cuts"
        except Exception:
            return False
    return False


def _check_edge_cuts_closed(board: object, edge_layer_id: int | None) -> tuple[bool, str]:
    if not hasattr(board, "GetDrawings"):
        return False, "Board API does not expose drawings for outline validation."

    edge_items = 0
    endpoint_counts: dict[tuple[int, int], int] = {}
    closed_items = 0
    drawings = board.GetDrawings()
    for item in drawings:
        if not _is_edge_cuts_item(item, board, edge_layer_id):
            continue
        edge_items += 1
        if _is_closed_outline_item(item):
            closed_items += 1
            continue
        if hasattr(item, "GetStart") and hasattr(item, "GetEnd"):
            try:
                start_key = _point_key(item.GetStart())
                end_key = _point_key(item.GetEnd())
            except Exception:
                start_key = None
                end_key = None
            if start_key is not None and end_key is not None:
                endpoint_counts[start_key] = endpoint_counts.get(start_key, 0) + 1
                endpoint_counts[end_key] = endpoint_counts.get(end_key, 0) + 1

    if edge_items == 0:
        return False, "No Edge.Cuts geometry found."
    if closed_items > 0 and not endpoint_counts:
        return True, ""
    if not endpoint_counts:
        return False, "Edge.Cuts exists but no drawable outline segments were found."

    unmatched = sum(1 for count in endpoint_counts.values() if count % 2 != 0)
    if unmatched:
        return False, f"Edge.Cuts outline appears open ({unmatched} unmatched endpoints)."
    return True, ""


def _validate_board_preflight(board_path: Path) -> tuple[bool, str]:
    """Validate board loadability and basic outline integrity before routing."""
    pcbnew = _get_pcbnew()
    if pcbnew is not None:
        try:
            board = pcbnew.LoadBoard(str(board_path))
            if board is None:
                return False, _malformed_board_message(board_path)
            edge_layer_id = getattr(pcbnew, "Edge_Cuts", None)
            is_closed, details = _check_edge_cuts_closed(board, edge_layer_id)
            if not is_closed:
                return False, _board_outline_error_message(board_path, details)
            return True, ""
        except Exception as e:
            return False, _format_pcbnew_error("Board preflight failed", str(e), board_path)

    script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(board_path)!r})
if board is None:
    print("{_BOARD_LOAD_FAILED_SENTINEL}")
    sys.exit(2)

edge_layer_id = getattr(pcbnew, "Edge_Cuts", None)
edge_items = 0
endpoint_counts = {{}}
closed_items = 0

def point_key(point):
    x = getattr(point, "x", None)
    y = getattr(point, "y", None)
    if x is None or y is None:
        try:
            return int(point[0]), int(point[1])
        except Exception:
            return None
    return int(x), int(y)

def is_edge_cuts(item):
    if hasattr(item, "GetLayerName"):
        try:
            if item.GetLayerName() == "Edge.Cuts":
                return True
        except Exception:
            pass
    if hasattr(item, "GetLayer"):
        try:
            layer = int(item.GetLayer())
            if edge_layer_id is not None:
                return layer == edge_layer_id
            if hasattr(board, "GetLayerName"):
                return board.GetLayerName(layer) == "Edge.Cuts"
        except Exception:
            return False
    return False

def is_closed_item(item):
    if hasattr(item, "IsClosed"):
        try:
            if bool(item.IsClosed()):
                return True
        except Exception:
            pass
    if hasattr(item, "GetShapeStr"):
        try:
            shape = str(item.GetShapeStr()).lower()
            if ("rect" in shape or "polygon" in shape or "poly" in shape or "circle" in shape):
                return True
        except Exception:
            pass
    return False

if not hasattr(board, "GetDrawings"):
    print("{_BOARD_OUTLINE_NO_SEGMENTS_SENTINEL}")
    sys.exit(4)

for item in board.GetDrawings():
    if not is_edge_cuts(item):
        continue
    edge_items += 1
    if is_closed_item(item):
        closed_items += 1
        continue
    if hasattr(item, "GetStart") and hasattr(item, "GetEnd"):
        try:
            start_key = point_key(item.GetStart())
            end_key = point_key(item.GetEnd())
        except Exception:
            start_key = None
            end_key = None
        if start_key is not None and end_key is not None:
            endpoint_counts[start_key] = endpoint_counts.get(start_key, 0) + 1
            endpoint_counts[end_key] = endpoint_counts.get(end_key, 0) + 1

if edge_items == 0:
    print("{_BOARD_OUTLINE_MISSING_SENTINEL}")
    sys.exit(3)

if closed_items > 0 and not endpoint_counts:
    print("BOARD_PREFLIGHT_OK")
    sys.exit(0)

if not endpoint_counts:
    print("{_BOARD_OUTLINE_NO_SEGMENTS_SENTINEL}")
    sys.exit(4)

unmatched = sum(1 for count in endpoint_counts.values() if count % 2 != 0)
if unmatched:
    print(f"{_BOARD_OUTLINE_OPEN_SENTINEL}:{{unmatched}}")
    sys.exit(5)

print("BOARD_PREFLIGHT_OK")
"""
    ok, output_text = _run_pcbnew_script(script, timeout=_BOARD_PREFLIGHT_TIMEOUT_SECONDS)
    if ok and "BOARD_PREFLIGHT_OK" in output_text:
        return True, ""

    output_text = output_text or ""
    if _BOARD_LOAD_FAILED_SENTINEL in output_text:
        return False, _malformed_board_message(board_path)
    if _BOARD_OUTLINE_MISSING_SENTINEL in output_text:
        return False, _board_outline_error_message(board_path, "No Edge.Cuts geometry found.")
    if _BOARD_OUTLINE_NO_SEGMENTS_SENTINEL in output_text:
        return False, _board_outline_error_message(
            board_path,
            "Edge.Cuts exists but no drawable outline segments were found.",
        )
    if _BOARD_OUTLINE_OPEN_SENTINEL in output_text:
        detail = "Edge.Cuts outline appears open."
        for line in output_text.splitlines():
            if line.startswith(f"{_BOARD_OUTLINE_OPEN_SENTINEL}:"):
                unmatched = line.split(":", 1)[1].strip()
                if unmatched.isdigit():
                    detail = f"Edge.Cuts outline appears open ({unmatched} unmatched endpoints)."
                break
        return False, _board_outline_error_message(board_path, detail)

    return False, _format_pcbnew_error("Board preflight failed", output_text, board_path)


# ---------------------------------------------------------------------------
# SubprocessBoardOps — implements export_dsn / import_ses via pcbnew subprocess
# ---------------------------------------------------------------------------

class SubprocessBoardOps(BoardOps):
    """Board ops that run pcbnew in a subprocess for DSN/SES operations.

    Only export_dsn and import_ses are implemented; all other BoardOps methods
    raise NotImplementedError (use a different backend for read/modify ops).
    """

    # -- Unimplemented abstract methods --

    def read_board(self, path: Path) -> dict[str, Any]:
        raise NotImplementedError("SubprocessBoardOps does not support read_board")

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        raise NotImplementedError("SubprocessBoardOps does not support get_components")

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        raise NotImplementedError("SubprocessBoardOps does not support get_nets")

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        raise NotImplementedError("SubprocessBoardOps does not support get_tracks")

    def get_board_info(self, path: Path) -> dict[str, Any]:
        raise NotImplementedError("SubprocessBoardOps does not support get_board_info")

    # -- DSN/SES routing operations --

    def export_dsn(self, path: Path, dsn_path: Path) -> dict[str, Any]:
        """Export board to Specctra DSN via pcbnew subprocess.

        Args:
            path: Path to the validated .kicad_pcb file.
            dsn_path: Resolved output path for the .dsn file.

        Returns:
            Dict with dsn_path, size_bytes, message keys.

        Raises:
            RuntimeError: On preflight failure or pcbnew export error.
        """
        preflight_ok, preflight_message = _validate_board_preflight(path)
        if not preflight_ok:
            raise RuntimeError(preflight_message)

        dsn_path.parent.mkdir(parents=True, exist_ok=True)

        pcbnew = _get_pcbnew()
        if pcbnew is not None:
            try:
                board = pcbnew.LoadBoard(str(path))
                if board is None:
                    raise RuntimeError(_malformed_board_message(path))
                ok = pcbnew.ExportSpecctraDSN(board, str(dsn_path))
                if not ok:
                    raise RuntimeError(
                        "ExportSpecctraDSN returned False. "
                        "Check for duplicate reference designators."
                    )
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(
                    _format_pcbnew_error("pcbnew DSN export failed", str(e), path)
                ) from e
        else:
            script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(path)!r})
if board is None:
    print("{_BOARD_LOAD_FAILED_SENTINEL}")
    sys.exit(2)
ok = pcbnew.ExportSpecctraDSN(board, {str(dsn_path)!r})
if not ok:
    print("EXPORT_FAILED")
    sys.exit(1)
print("EXPORT_OK")
"""
            ok, output_text = _run_pcbnew_script(script, timeout=_DSN_EXPORT_TIMEOUT_SECONDS)
            if not ok or "EXPORT_FAILED" in output_text:
                raise RuntimeError(
                    _format_pcbnew_error("DSN export failed", output_text, path)
                )

        # Clean Unicode characters that FreeRouting cannot handle
        if dsn_path.exists():
            content = dsn_path.read_text(encoding="utf-8")
            cleaned = re.sub("[ΩµΦ°]", "", content)
            dsn_path.write_text(cleaned, encoding="utf-8")

        if not dsn_path.exists():
            raise RuntimeError("DSN file was not created")

        size = dsn_path.stat().st_size
        return {
            "dsn_path": str(dsn_path),
            "size_bytes": size,
            "message": f"DSN exported: {dsn_path.name} ({size} bytes)",
        }

    def import_ses(self, path: Path, ses_path: Path) -> dict[str, Any]:
        """Import Specctra SES routing into the PCB via pcbnew subprocess.

        Args:
            path: Path to the validated .kicad_pcb file.
            ses_path: Resolved path to the .ses session file.

        Returns:
            Dict with tracks_before, tracks_after, new_tracks, message keys.

        Raises:
            RuntimeError: On SES not found or pcbnew import error.
        """
        if not ses_path.exists():
            raise RuntimeError(f"SES file not found: {ses_path}")

        tracks_before = 0
        tracks_after = 0

        pcbnew = _get_pcbnew()
        if pcbnew is not None:
            try:
                board = pcbnew.LoadBoard(str(path))
                if board is None:
                    raise RuntimeError(_malformed_board_message(path))
                tracks_before = len(board.GetTracks())
                ok = pcbnew.ImportSpecctraSES(board, str(ses_path))
                if not ok:
                    raise RuntimeError("ImportSpecctraSES returned False")
                tracks_after = len(board.GetTracks())
                pcbnew.SaveBoard(str(path), board)
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(
                    _format_pcbnew_error("SES import failed", str(e), path)
                ) from e
        else:
            script = f"""
import pcbnew, sys
board = pcbnew.LoadBoard({str(path)!r})
if board is None:
    print("{_BOARD_LOAD_FAILED_SENTINEL}")
    sys.exit(2)
before = len(board.GetTracks())
ok = pcbnew.ImportSpecctraSES(board, {str(ses_path)!r})
if not ok:
    print("IMPORT_FAILED")
    sys.exit(1)
after = len(board.GetTracks())
pcbnew.SaveBoard({str(path)!r}, board)
print(f"TRACKS_BEFORE={{before}}")
print(f"TRACKS_AFTER={{after}}")
"""
            ok, output_text = _run_pcbnew_script(script, timeout=_SES_IMPORT_TIMEOUT_SECONDS)
            if not ok or "IMPORT_FAILED" in output_text:
                raise RuntimeError(
                    _format_pcbnew_error("SES import failed", output_text, path)
                )
            for line in output_text.splitlines():
                if line.startswith("TRACKS_BEFORE="):
                    tracks_before = int(line.split("=")[1])
                elif line.startswith("TRACKS_AFTER="):
                    tracks_after = int(line.split("=")[1])

        new_tracks = tracks_after - tracks_before
        return {
            "tracks_before": tracks_before,
            "tracks_after": tracks_after,
            "new_tracks": new_tracks,
            "message": f"Imported {new_tracks} routed tracks",
        }


# ---------------------------------------------------------------------------
# SubprocessBackend — advertises BOARD_ROUTE capability
# ---------------------------------------------------------------------------

class SubprocessBackend(KiCadBackend):
    """Backend that provides DSN/SES routing via KiCad's bundled Python subprocess.

    Advertises only BOARD_ROUTE.  All other capabilities use other backends.
    """

    @property
    def name(self) -> str:
        return "pcbnew_subprocess"

    @property
    def capabilities(self) -> set[BackendCapability]:
        return {BackendCapability.BOARD_ROUTE}

    def is_available(self) -> bool:
        """Check if KiCad's Python interpreter exists (path check, no subprocess)."""
        return _get_kicad_python() is not None

    def get_board_ops(self) -> SubprocessBoardOps:
        return SubprocessBoardOps()
