"""Subprocess helper utilities for pcbnew operations.

These functions are used by routing.py's clean_board_for_routing and
autoroute tools when pcbnew is not importable in the current Python
environment.  They locate KiCad's bundled Python interpreter and run
pcbnew scripts in a subprocess.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from kicad_mcp.logging_config import get_logger

logger = get_logger("backend.subprocess")


# ---------------------------------------------------------------------------
# Sentinel strings used in pcbnew subprocess output
# ---------------------------------------------------------------------------

_BOARD_LOAD_FAILED_SENTINEL = "BOARD_LOAD_FAILED"
_BOARD_CLEAN_TIMEOUT_SECONDS = 300


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
        kicad_in_path = shutil.which("kicad")
        if kicad_in_path:
            real = Path(kicad_in_path).resolve()
            snap_python = real.parent / "python3"
            if snap_python.exists():
                return snap_python

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
        from kicad_mcp.utils.platform_helper import find_kicad_python_paths
        kicad_paths = [str(p) for p in find_kicad_python_paths()]
        if kicad_paths:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = ":".join(kicad_paths + ([existing] if existing else []))
    else:  # linux
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
