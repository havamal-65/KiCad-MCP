"""REQ-FR-4 (F3/S4, #22) — a FreeRouting timeout returns a structured result.

On timeout the router must not hang silently or collapse to a bare error: it
returns ``{status: "timeout", elapsed_s, timeout_s, max_passes,
detected_version, hint}`` so the caller can act (lower max_passes / raise the
limit).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_mcp.tools.routing import _impl_run_freerouter
from kicad_mcp.utils.change_log import ChangeLog


def test_timeout_returns_structured_result(tmp_path: Path):
    java = tmp_path / "java.exe"
    java.touch()
    jar = tmp_path / "freerouting-2.2.4.jar"
    jar.touch()
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(PCB)", encoding="utf-8")
    ses = tmp_path / "board.ses"

    proc = MagicMock()
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="freerouting", timeout=120),
        (b"", b""),  # the post-kill drain call
    ]
    cfg = SimpleNamespace(
        java_path=None, freerouting_jar=None, freerouting_timeout_seconds=120,
    )
    with patch("kicad_mcp.tools.routing.find_java", return_value=java), \
         patch("kicad_mcp.tools.routing.find_freerouting_jar", return_value=jar), \
         patch("kicad_mcp.utils.platform_helper.detect_java_major_version", return_value=25), \
         patch("kicad_mcp.tools.routing.subprocess.Popen", return_value=proc):
        result = json.loads(_impl_run_freerouter(
            dsn_path=str(dsn), output=str(ses), max_passes=50,
            freerouting_jar="", java_path="",
            config=cfg, change_log=ChangeLog(tmp_path / "c.json"),
        ))

    assert result["status"] == "timeout"
    assert result["timeout_s"] == 120
    assert result["max_passes"] == 50
    assert result["detected_version"] == "2.2.4"
    assert "hint" in result and result["hint"]
    assert "elapsed_s" in result
    proc.kill.assert_called_once()
