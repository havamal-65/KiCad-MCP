"""Units for the K10-S3 capability gate (utils/kicad_capabilities.py).

All subprocess traffic is mocked — no KiCad required (V-A). Covers the
--help probe verdicts, the version-compare fallback, per-path caching, and
the exact REQ-FEAT-002 refusal payload shape.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.models.errors import KicadCapabilityError
from kicad_mcp.utils import kicad_capabilities as kc

CLI_10 = Path(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")
CLI_9 = Path(r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe")

# Trimmed real-world shapes of `kicad-cli sch export bom --help`.
HELP_WITH_VARIANT = """\
Usage: kicad-cli sch export bom [--help] [--output OUTPUT_FILE]
       [--variant VAR] [--exclude-dnp] [--fields FIELDS] input
"""
HELP_WITHOUT_VARIANT = """\
Usage: kicad-cli sch export bom [--help] [--output OUTPUT_FILE]
       [--fields FIELDS] [--exclude-dnp] input
"""


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    kc.clear_capability_cache()


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    help_text: str | None,
    version: str = "10.0.4",
) -> list[list[str]]:
    """Route the module's subprocess.run: --help → *help_text* (None = OSError),
    --version → *version*. Returns the list of argv lists actually run."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        calls.append(cmd)
        if cmd[-1] == "--help":
            if help_text is None:
                raise OSError("cannot run --help")
            return _FakeCompleted(stdout=help_text)
        if cmd[-1] == "--version":
            return _FakeCompleted(stdout=version + "\n")
        raise AssertionError(f"unexpected argv: {cmd}")

    monkeypatch.setattr(kc.subprocess, "run", fake_run)
    return calls


# ── cli_supports: the --help probe ───────────────────────────────────────────

def test_supports_when_help_lists_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT)
    assert kc.cli_supports("variant-exports", CLI_10) is True


def test_not_supported_when_help_lacks_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=HELP_WITHOUT_VARIANT, version="9.0.7")
    assert kc.cli_supports("variant-exports", CLI_9) is False


def test_help_probe_beats_version_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hypothetical 9.x with a backported --variant must gate OPEN (REQ-FEAT-001):
    # capability probing, not version comparison, is the primary check.
    _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT, version="9.0.7")
    assert kc.cli_supports("variant-exports", CLI_9) is True


def test_unknown_capability_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT)
    with pytest.raises(ValueError, match="Unknown capability"):
        kc.cli_supports("teleportation", CLI_10)


# ── version-compare fallback (help probe unrunnable) ─────────────────────────

def test_fallback_version_10_supports(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=None, version="10.0.4")
    assert kc.cli_supports("variant-exports", CLI_10) is True


def test_fallback_version_9_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=None, version="9.0.7")
    assert kc.cli_supports("variant-exports", CLI_9) is False


def test_fallback_unparseable_version_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=None, version="mystery build")
    assert kc.cli_supports("variant-exports", CLI_10) is False


def test_version_timeout_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd, kc._PROBE_TIMEOUT)

    monkeypatch.setattr(kc.subprocess, "run", fake_run)
    assert kc.cli_version_echo(CLI_10) == "unknown"


# ── caching (one probe per capability × path) ────────────────────────────────

def test_probe_cached_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT)
    assert kc.cli_supports("variant-exports", CLI_10)
    assert kc.cli_supports("variant-exports", CLI_10)
    assert len(calls) == 1  # second call served from cache

    # A different resolved path is a different binary — must probe again.
    kc.cli_supports("variant-exports", CLI_9)
    assert len(calls) == 2


def test_clear_cache_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT)
    kc.cli_supports("variant-exports", CLI_10)
    kc.clear_capability_cache()
    kc.cli_supports("variant-exports", CLI_10)
    assert len(calls) == 2


# ── requires_kicad: the structured refusal (REQ-FEAT-002 / REQ-S3F-003) ─────

def test_requires_passes_silently_on_10(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=HELP_WITH_VARIANT)
    kc.requires_kicad("variant-exports", CLI_10)  # no raise


def test_requires_raises_structured_payload_on_9(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, help_text=HELP_WITHOUT_VARIANT, version="9.0.7")
    with pytest.raises(KicadCapabilityError) as exc_info:
        kc.requires_kicad("variant-exports", CLI_9)
    err = exc_info.value
    assert err.payload == {
        "error": "capability_unavailable",
        "capability": "variant-exports",
        "required": "kicad-cli 10.0+ (--variant)",
        "detected": "9.0.7",
        "hint": "set KICAD_MCP_KICAD_VERSION=10.0 or install KiCad 10",
    }
    # payload is also the KiCadMCPError details dict (tool layer contract)
    assert err.details == err.payload
    assert "9.0.7" in str(err)
