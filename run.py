#!/usr/bin/env python3
"""Cross-platform launcher for kicad-mcp.

Creates a virtual environment on first run, installs kicad-mcp into it,
then executes the server. Works on Windows, macOS, and Linux without any
platform-specific shell (no PowerShell or bash required).

Usage:
    python3 run.py
    python3 run.py --transport sse --port 8765
    python3 run.py --check
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_DIR / ".venv"

if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"


def ensure_venv() -> None:
    if not VENV_PYTHON.exists():
        print(f"[kicad-mcp] Creating virtual environment at {VENV_DIR} ...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)

    result = subprocess.run(
        [str(VENV_PYTHON), "-c", "import kicad_mcp"],
        capture_output=True,
    )
    if result.returncode != 0:
        print("[kicad-mcp] Installing kicad-mcp into venv...", file=sys.stderr)
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "-e", str(PROJECT_DIR), "--quiet"],
            check=True,
        )


ensure_venv()

cmd = [str(VENV_PYTHON), "-m", "kicad_mcp"] + sys.argv[1:]
if sys.platform == "win32":
    sys.exit(subprocess.run(cmd).returncode)
else:
    os.execv(str(VENV_PYTHON), cmd)
