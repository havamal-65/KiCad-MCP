"""REQ-FR-1 (F3/S4, #21) — the FreeRouting timeout is configurable.

The subprocess timeout must come from ``config.freerouting_timeout_seconds``
(default 300, override via KICAD_MCP_FREEROUTING_TIMEOUT_SECONDS), not a
hardcoded value. The config field itself shipped in b65df77; this pins that
``_impl_run_freerouter`` actually reads it and that the env var flows through.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.tools.routing import _impl_run_freerouter
from kicad_mcp.utils.change_log import ChangeLog


def _run_and_capture_timeout(tmp_path: Path, timeout_s: int) -> int:
    java = tmp_path / "java.exe"
    java.touch()
    jar = tmp_path / "freerouting-2.2.4.jar"
    jar.touch()
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(PCB)", encoding="utf-8")
    ses = tmp_path / "board.ses"
    ses.write_text("(session)\n", encoding="utf-8")

    proc = MagicMock()
    proc.communicate.return_value = (b"Freerouting v2.2.4\n(0 unrouted)\n", b"")
    cfg = SimpleNamespace(
        java_path=None, freerouting_jar=None,
        freerouting_timeout_seconds=timeout_s,
    )
    with patch("kicad_mcp.tools.routing.find_java", return_value=java), \
         patch("kicad_mcp.tools.routing.find_freerouting_jar", return_value=jar), \
         patch("kicad_mcp.utils.platform_helper.detect_java_major_version", return_value=25), \
         patch("kicad_mcp.tools.routing.subprocess.Popen", return_value=proc):
        _impl_run_freerouter(
            dsn_path=str(dsn), output=str(ses), max_passes=3,
            freerouting_jar="", java_path="",
            config=cfg, change_log=ChangeLog(tmp_path / "c.json"),
        )
    # communicate(timeout=...) is keyword-only in the impl.
    return proc.communicate.call_args.kwargs["timeout"]


def test_impl_uses_config_timeout(tmp_path: Path):
    assert _run_and_capture_timeout(tmp_path, 450) == 450


def test_config_default_is_300(monkeypatch):
    monkeypatch.delenv("KICAD_MCP_FREEROUTING_TIMEOUT_SECONDS", raising=False)
    cfg = KiCadMCPConfig(_env_file=None)
    assert cfg.freerouting_timeout_seconds == 300


def test_config_reads_env_override(monkeypatch):
    monkeypatch.setenv("KICAD_MCP_FREEROUTING_TIMEOUT_SECONDS", "600")
    cfg = KiCadMCPConfig(_env_file=None)
    assert cfg.freerouting_timeout_seconds == 600
