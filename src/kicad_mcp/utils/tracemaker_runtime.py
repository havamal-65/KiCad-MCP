"""TraceMaker autorouter runtime — detection and invocation (TM Stage 1, unit 1).

TraceMaker (github.com/DingoOz/TraceMaker) is a Linux-only C++ autorouter
with native ``.kicad_pcb`` in/out (``tracemaker route in.kicad_pcb -o
out.kicad_pcb``). On Windows it runs inside WSL2, invoked as
``wsl.exe -d <distro> -e bash -lc "<binary> route /mnt/d/... -o /mnt/d/..."``;
on Linux/macOS the binary is invoked directly.

This module is the single place that knows how to find and run TraceMaker.
It never raises on subprocess failure: detection returns ``None`` and
``run_tracemaker`` returns a structured status dict, so callers can fall
back to FreeRouting without try/except plumbing.

Notes:
- The detection cache is NOT thread-locked. A concurrent first call may
  double-probe; both probes compute the same answer, so the race is benign.
- An EMPTY-string ``KICAD_MCP_TRACEMAKER_DISTRO`` / ``KICAD_MCP_TRACEMAKER_BIN``
  falls back to the built-in default, same as unset.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("utils.tracemaker_runtime")

ENV_DISTRO = "KICAD_MCP_TRACEMAKER_DISTRO"
ENV_BIN = "KICAD_MCP_TRACEMAKER_BIN"
ENV_DISABLE = "KICAD_MCP_NO_TRACEMAKER"

_DEFAULT_DISTRO = "tracemaker-build"
_DEFAULT_BIN = "~/TraceMaker/build/cpu-only/src/app/tracemaker"

_PROBE_TIMEOUT_S = 20
_TAIL_CHARS = 2000

_PKILL_TIMEOUT_S = 10

# Optional "v" prefix so "v0.9.1-rc1" parses as "0.9.1-rc1", not "9.1-rc1".
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?)\b")

# Best-effort: TraceMaker's summary line. A number only counts when it is
# ADJACENT to an unrouted/connections/nets noun ("Unrouted: 2", "2 unrouted",
# "2 connections ... incomplete"); a bare pass/iteration count near
# "incomplete" ("incomplete after 42 passes") must NOT match — None when
# unsure.
_UNROUTED_PATTERNS = (
    # "Unrouted: 42" / "unrouted = 42" — only punctuation/space between
    re.compile(r"\bunrouted\b[\s:=-]{0,4}(\d+)\b", re.IGNORECASE),
    # "42 unrouted" / "42 connections unrouted" / "42 nets unrouted"
    re.compile(r"\b(\d+)\s{1,4}(?:connections?\s+|nets?\s+)?unrouted\b", re.IGNORECASE),
    # "42 connections ... incomplete" / "3 nets incomplete"
    re.compile(
        r"\b(\d+)\s{1,4}(?:connections?|nets?)\b[^\n\d]{0,40}?\bincomplete\b",
        re.IGNORECASE,
    ),
    # "incomplete: 42 connections" / "incomplete: 3 nets"
    re.compile(
        r"\bincomplete\b[\s:=-]{0,4}(\d+)\s{1,4}(?:connections?|nets?)\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class TracemakerRuntime:
    """A usable TraceMaker install, as confirmed by a version probe.

    TraceMaker has no ``--version`` flag (it exits non-zero, "A subcommand
    is required"); the probe is the ``version`` SUBCOMMAND, whose stdout is
    "tracemaker X.Y.Z" with exit 0.
    """

    mode: str  # "wsl" (win32) | "native" (linux/macos)
    distro: str | None  # WSL distro name when mode == "wsl", else None
    binary: str  # binary path (WSL-side path or native path)
    version: str | None  # parsed from `tracemaker version` probe output, else None


# Per-process detection cache. ``_probed`` distinguishes "never probed"
# from "probed and found nothing" so a missing install is not re-probed
# (the WSL probe can take seconds) — detect_tracemaker(force=True) re-probes.
_probed: bool = False
_cached_runtime: TracemakerRuntime | None = None


def tracemaker_disabled() -> bool:
    """True when ``KICAD_MCP_NO_TRACEMAKER`` is set non-empty and not "0"."""
    value = os.environ.get(ENV_DISABLE, "")
    return bool(value) and value != "0"


def _creationflags() -> int:
    # Windows-only attr accessed via getattr: CI mypy runs --platform linux.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _tail(output: str | bytes | None) -> str:
    """Last ``_TAIL_CHARS`` characters of a subprocess stream, "" for None."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output[-_TAIL_CHARS:]


def _parse_unrouted(stdout: str) -> int | None:
    """Best-effort unrouted/incomplete count from TraceMaker's stdout."""
    for pattern in _UNROUTED_PATTERNS:
        match = pattern.search(stdout)
        if match:
            try:
                return int(match.group(1))
            except ValueError:  # pragma: no cover - \d+ always parses
                return None
    return None


def detect_tracemaker(force: bool = False) -> TracemakerRuntime | None:
    """Probe for a working TraceMaker install; None when disabled or absent.

    The probe runs ``<binary> version`` (the subcommand — there is no
    ``--version`` flag) and expects exit 0 with "tracemaker X.Y.Z" on
    stdout. The result (including a failed probe) is cached per process;
    ``force=True`` discards the cache and re-probes. Never raises — any
    probe failure (missing wsl.exe/binary, nonzero exit, timeout) is None.
    """
    global _probed, _cached_runtime

    if tracemaker_disabled():
        return None
    if _probed and not force:
        return _cached_runtime

    distro = os.environ.get(ENV_DISTRO) or _DEFAULT_DISTRO
    binary = os.environ.get(ENV_BIN) or _DEFAULT_BIN

    if sys.platform == "win32":
        mode = "wsl"
        rt_distro: str | None = distro
        rt_binary = binary
        cmd = [
            "wsl.exe",
            "-d",
            distro,
            "-e",
            "bash",
            "-lc",
            f"test -x {binary} && {binary} version",
        ]
    else:
        mode = "native"
        rt_distro = None
        # os.path.expanduser (not Path.expanduser): it only rewrites a
        # leading "~" and never re-flavors the separators of an already
        # absolute POSIX path.
        rt_binary = os.path.expanduser(binary)
        cmd = [rt_binary, "version"]

    runtime: TracemakerRuntime | None = None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            creationflags=_creationflags(),
        )
    except Exception as exc:  # probe must never raise
        logger.debug("TraceMaker probe failed: %s", exc)
    else:
        if proc.returncode == 0:
            version = _parse_version(proc.stdout or "") or _parse_version(proc.stderr or "")
            runtime = TracemakerRuntime(
                mode=mode, distro=rt_distro, binary=rt_binary, version=version
            )
            logger.info(
                "TraceMaker detected: mode=%s distro=%s version=%s",
                mode,
                rt_distro,
                version,
            )
        else:
            logger.debug(
                "TraceMaker probe exited %s: %s",
                proc.returncode,
                _tail(proc.stderr)[-200:],
            )

    _cached_runtime = runtime
    _probed = True
    return _cached_runtime


def to_wsl_path(p: Path) -> str:
    r"""Translate a Windows drive path to its WSL mount: D:\x\y -> /mnt/d/x/y.

    Raises ValueError for anything without a plain ``<letter>:`` drive
    anchor (UNC shares, relative paths) — WSL cannot mount those under
    /mnt/<letter>.
    """
    win = PureWindowsPath(p)
    if not re.fullmatch(r"[A-Za-z]:", win.drive) or not win.root:
        raise ValueError(f"not a Windows drive-letter path, cannot map to WSL: {p}")
    tail = "/".join(win.parts[1:])  # parts[0] is the "D:\" anchor
    return f"/mnt/{win.drive[0].lower()}/{tail}" if tail else f"/mnt/{win.drive[0].lower()}"


def run_tracemaker(
    rt: TracemakerRuntime,
    pcb_in: Path,
    pcb_out: Path,
    *,
    work: int | None = None,
    time_limit_s: int | None = None,
    timeout_s: int = 900,
) -> dict[str, Any]:
    """Route ``pcb_in`` into ``pcb_out`` with TraceMaker; structured result.

    Returns ``{"status": "success"|"error"|"timeout", "returncode": int|None,
    "wall_time_s": float, "stdout_tail": str, "stderr_tail": str,
    "unrouted": int|None}``. "success" requires exit code 0 AND an existing
    output file. Never raises on subprocess failure. On timeout,
    ``subprocess.run``'s timeout handling kills the direct child — in wsl
    mode that is ``wsl.exe``, NOT the Linux-side tracemaker process, so a
    best-effort ``pkill -f tracemaker`` is then issued inside the distro
    (short timeout, all errors swallowed) — and status is "timeout".
    """
    if rt.mode == "wsl":
        # Quote the translated paths (spaces, "$", "&", ... survive the
        # bash -lc string); rt.binary stays unquoted so "~" tilde-expands.
        inner = (
            f"{rt.binary} route {shlex.quote(to_wsl_path(pcb_in))}"
            f" -o {shlex.quote(to_wsl_path(pcb_out))}"
        )
        if work is not None:
            inner += f" --work {int(work)}"
        if time_limit_s is not None:
            inner += f" --time {int(time_limit_s)}"
        cmd = ["wsl.exe", "-d", rt.distro or _DEFAULT_DISTRO, "-e", "bash", "-lc", inner]
    else:
        cmd = [rt.binary, "route", str(pcb_in), "-o", str(pcb_out)]
        if work is not None:
            cmd += ["--work", str(int(work))]
        if time_limit_s is not None:
            cmd += ["--time", str(int(time_limit_s))]

    logger.info("Running TraceMaker: %s", " ".join(cmd))
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        wall = time.monotonic() - start
        logger.warning("TraceMaker timed out after %.1fs (limit %ss)", wall, timeout_s)
        if rt.mode == "wsl":
            # run()'s timeout killed wsl.exe only; the Linux-side process
            # may still be running inside the distro. Best-effort cleanup.
            try:
                subprocess.run(
                    [
                        "wsl.exe",
                        "-d",
                        rt.distro or _DEFAULT_DISTRO,
                        "-e",
                        "bash",
                        "-lc",
                        "pkill -f tracemaker || true",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_PKILL_TIMEOUT_S,
                    creationflags=_creationflags(),
                )
            except Exception as cleanup_exc:
                logger.debug("TraceMaker post-timeout cleanup failed: %s", cleanup_exc)
        return {
            "status": "timeout",
            "returncode": None,
            "wall_time_s": round(wall, 2),
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
            "unrouted": None,
        }
    except Exception as exc:  # launch failure (missing wsl.exe/binary, ...)
        wall = time.monotonic() - start
        logger.warning("TraceMaker failed to run: %s", exc)
        return {
            "status": "error",
            "returncode": None,
            "wall_time_s": round(wall, 2),
            "stdout_tail": "",
            "stderr_tail": _tail(str(exc)),
            "unrouted": None,
        }

    wall = time.monotonic() - start
    stdout = proc.stdout or ""
    success = proc.returncode == 0 and pcb_out.exists()
    if proc.returncode == 0 and not pcb_out.exists():
        logger.warning("TraceMaker exited 0 but wrote no output: %s", pcb_out)
    return {
        "status": "success" if success else "error",
        "returncode": proc.returncode,
        "wall_time_s": round(wall, 2),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(proc.stderr),
        "unrouted": _parse_unrouted(stdout),
    }
