"""Capability gate for the *serving* kicad-cli (K10-S3, REQ-S3F-001..003).

A tool that accepts a v10-gated parameter (e.g. ``variant`` on the export
tools) asks this module "can the CLI that will actually run this export do
it?" — never "is this KiCad 10". The primary check greps the serving CLI's
own ``--help`` for the capability's flag (REQ-FEAT-001: a backported flag
gates correctly where a version compare would not); a version compare from
``--version`` is only the fallback when the help probe itself fails.

Keying rule (REQ-S3F-002): callers pass the cli path they resolved for the
export (``find_kicad_cli()`` under the current env) — this module never
re-resolves a preference of its own. Probe results are cached per resolved
path for the process lifetime; ``clear_capability_cache()`` resets (tests,
launcher version switch).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict

from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import KicadCapabilityError

logger = get_logger("kicad_capabilities")

_PROBE_TIMEOUT = 30  # seconds; --help/--version are fast, generous for cold AV scans


class _CapabilitySpec(TypedDict):
    """How to detect one capability on a kicad-cli binary."""

    help_args: list[str]  # subcommand whose --help carries the flag
    flag: str  # the flag whose presence proves the capability
    required: str  # human string for the refusal payload
    min_major: int  # --version fallback threshold (help probe failed)


# The only capability registry (AR1-family: no version lists at call sites).
_CAPABILITIES: dict[str, _CapabilitySpec] = {
    "variant-exports": {
        "help_args": ["sch", "export", "bom", "--help"],
        "flag": "--variant",
        "required": "kicad-cli 10.0+ (--variant)",
        "min_major": 10,
    },
}

# (capability, resolved cli path) -> probe verdict; per-process, cleared on demand.
_probe_cache: dict[tuple[str, str], bool] = {}
# resolved cli path -> `--version` echo (or "unknown"), for refusal payloads.
_version_cache: dict[str, str] = {}


def clear_capability_cache() -> None:
    """Reset all cached probe results (tests; launcher version switch)."""
    _probe_cache.clear()
    _version_cache.clear()


def _run_cli(cli_path: Path, args: list[str]) -> str | None:
    """Run the CLI and return combined stdout+stderr, or None on any failure."""
    try:
        result = subprocess.run(
            [str(cli_path), *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("capability probe failed to run %s %s: %s", cli_path, args, exc)
        return None
    return (result.stdout or "") + (result.stderr or "")


def cli_version_echo(cli_path: Path) -> str:
    """The CLI's ``--version`` echo (cached), or ``"unknown"``.

    Used verbatim as the ``detected`` field of a refusal payload so the user
    sees exactly what the serving binary reports.
    """
    key = str(cli_path)
    if key not in _version_cache:
        out = _run_cli(cli_path, ["--version"])
        _version_cache[key] = out.strip() if out and out.strip() else "unknown"
    return _version_cache[key]


def _version_at_least(cli_path: Path, min_major: int) -> bool:
    """Fallback check: parse the leading major from ``--version``."""
    echo = cli_version_echo(cli_path)
    head = echo.split(".", 1)[0].strip()
    # `kicad-cli --version` echoes a bare version ("10.0.4"); tolerate a prefix.
    digits = "".join(ch for ch in head if ch.isdigit())
    if not digits:
        return False
    return int(digits) >= min_major


def cli_supports(capability: str, cli_path: Path) -> bool:
    """True iff *this* kicad-cli binary provides *capability*.

    Primary: run the registry's ``--help`` probe and look for the flag.
    Fallback (help probe unrunnable): ``--version`` major compare. Cached per
    (capability, resolved path).
    """
    spec = _CAPABILITIES.get(capability)
    if spec is None:
        raise ValueError(f"Unknown capability: {capability!r}")
    key = (capability, str(cli_path))
    if key in _probe_cache:
        return _probe_cache[key]

    help_text = _run_cli(cli_path, spec["help_args"])
    if help_text is not None:
        verdict = spec["flag"] in help_text
    else:
        verdict = _version_at_least(cli_path, spec["min_major"])
        logger.warning(
            "capability %s: --help probe unrunnable for %s; version fallback -> %s",
            capability, cli_path, verdict,
        )
    _probe_cache[key] = verdict
    return verdict


def requires_kicad(capability: str, cli_path: Path) -> None:
    """Raise :class:`KicadCapabilityError` unless the serving CLI has *capability*.

    The exception's ``payload`` is the REQ-FEAT-002 structured refusal the
    tool layer returns as its JSON error — never a silent fallback export.
    """
    if cli_supports(capability, cli_path):
        return
    spec = _CAPABILITIES[capability]
    detected = cli_version_echo(cli_path)
    raise KicadCapabilityError(
        f"{capability} requires {spec['required']}; "
        f"serving kicad-cli ({cli_path}) reports {detected}",
        capability=capability,
        required=spec["required"],
        detected=detected,
        hint="set KICAD_MCP_KICAD_VERSION=10.0 or install KiCad 10",
    )
