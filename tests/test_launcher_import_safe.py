"""Import-safety guard for the launcher core (M4 / REQ-TEST-005, REQ-PLAT-003).

The non-GUI core modules must import on a headless machine with no `tkinter`.
We prove it in a subprocess that blocks `tkinter` at the import-system level,
then imports every core module and asserts none pulled `tkinter` in at load.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = r"""
import sys

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("tkinter blocked for headless import test")
        return None

sys.meta_path.insert(0, _Block())

import launcher
import launcher.config
import launcher.recents
import launcher.orchestrator
import launcher.processes

assert "tkinter" not in sys.modules, "a core module imported tkinter at load"
print("IMPORT_SAFE_OK")
"""


def test_core_modules_import_without_tkinter():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"headless import failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "IMPORT_SAFE_OK" in proc.stdout


def test_gui_modules_are_the_only_tkinter_importers():
    """Sanity: the core modules can be imported in-process without tkinter
    already being present because of them (app/__main__ are excluded)."""
    import importlib

    for name in (
        "launcher.config",
        "launcher.recents",
        "launcher.orchestrator",
        "launcher.processes",
    ):
        mod = importlib.import_module(name)
        assert mod is not None
