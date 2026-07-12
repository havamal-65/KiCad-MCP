"""Regression: _get_kicad_python() must not return a pcbnew-less interpreter.

On a headless Linux runner (e.g. CI) with no KiCad installed, the generic
`/usr/bin/python3` fallback exists but has no `pcbnew`. Returning it made
pcbnew-gated tests (test_ses_value_persistence_regression) *run* and then
crash on `import pcbnew` instead of skipping. The detector now probes
importability for the generic fallbacks before trusting them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from kicad_mcp.backends import subprocess_backend as sb


def test_probe_false_for_python_without_pcbnew():
    # The test-runner interpreter has no pcbnew module.
    assert sb._python_can_import_pcbnew(Path(sys.executable)) is False


def test_probe_result_is_cached():
    sb._pcbnew_probe_cache.clear()
    p = Path(sys.executable)
    # First call populates the cache; a second must not re-probe (patched to blow up).
    assert sb._python_can_import_pcbnew(p) is False
    with patch.object(sb.subprocess, "run", side_effect=AssertionError("re-probed")):
        assert sb._python_can_import_pcbnew(p) is False


def test_get_kicad_python_rejects_pcbnewless_linux_fallback():
    """kicad not in PATH + a system python that can't import pcbnew → None."""
    with patch("kicad_mcp.utils.platform_helper.get_platform", return_value="linux"), \
         patch("shutil.which", return_value=None), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(sb, "_python_can_import_pcbnew", return_value=False):
        assert sb._get_kicad_python() is None


def test_get_kicad_python_accepts_pcbnew_capable_linux_fallback():
    """If the system python CAN import pcbnew (some distros), still return it."""
    with patch("kicad_mcp.utils.platform_helper.get_platform", return_value="linux"), \
         patch("shutil.which", return_value=None), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(sb, "_python_can_import_pcbnew", return_value=True):
        assert sb._get_kicad_python() == Path("/usr/bin/python3")
