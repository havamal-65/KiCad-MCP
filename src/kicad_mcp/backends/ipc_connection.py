"""IPC API connection lifecycle for the KiCad IPC board backend (F1 / S1).

Owns the kipy ``KiCad()`` handle and its liveness state — one instance per
backend (spec §2.1, bridge-board-access):

- ``connect()``      — establish/re-establish; raises :class:`IPCUnavailableError`
                       with remedy text on refuse (REQ-LIFE-1/2)
- ``is_available()`` — server reachable AND a loaded board open (REQ-ROUTE-3)
- ``ping()``         — health probe; a failed probe drops the handle so the
                       next call reconnects (REQ-LIFE-3)
- ``board()``        — Board handle for the open PCB document
- ``board_ready()``  — board loaded (real filename), not still-loading (REQ-GATE-1)
- ``reconnect()``    — full drop + fresh dial after a KiCad restart (C1b)

The ``kipy`` import is guarded (REQ-IPC-8): with the client library missing the
connection reports permanently unavailable and the router degrades to the SWIG
bridge / file paths — it never crashes the MCP server (REQ-LIFE-4).

Environment variables
---------------------
KICAD_MCP_IPC_ENABLED     "0"/"false" disables IPC routing entirely (default on)
KICAD_MCP_IPC_SOCKET      IPC socket path override (else kipy's KICAD_API_SOCKET
                          / platform default, e.g. ipc://%TEMP%\\kicad\\api.sock)
KICAD_MCP_IPC_TIMEOUT_MS  per-request timeout in milliseconds (default 2000)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import BackendNotAvailableError

if TYPE_CHECKING:
    from kipy.board import Board

try:
    import kipy
    import kipy.errors
except ImportError:  # REQ-IPC-8: missing client degrades, never crashes
    kipy = None  # type: ignore[assignment]

logger = get_logger("backend.ipc")

_DEFAULT_TIMEOUT_MS = 2000


class IPCUnavailableError(BackendNotAvailableError):
    """The KiCad IPC API cannot serve board ops right now.

    Carries actionable ``remedy`` text (REQ-LIFE-2) so callers can surface how
    to recover (enable the server pref / restart KiCad / open a board) instead
    of a raw connection error (REQ-SAFE-4).
    """

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, {"remedy": remedy} if remedy else None)
        self.remedy = remedy


def ipc_enabled() -> bool:
    """Operator kill-switch — KICAD_MCP_IPC_ENABLED=0 forces bridge/file paths."""
    raw = os.environ.get("KICAD_MCP_IPC_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _get_socket_path() -> str | None:
    return os.environ.get("KICAD_MCP_IPC_SOCKET") or None


def _get_timeout_ms() -> int:
    raw = os.environ.get("KICAD_MCP_IPC_TIMEOUT_MS", "")
    try:
        return int(raw) if raw else _DEFAULT_TIMEOUT_MS
    except ValueError:
        logger.warning("Ignoring non-integer KICAD_MCP_IPC_TIMEOUT_MS=%r", raw)
        return _DEFAULT_TIMEOUT_MS


def _kicad_config_root() -> Path | None:
    """Directory holding per-version KiCad config dirs (…/kicad/9.0/…)."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "kicad" if appdata else None
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "kicad"


def read_api_server_pref() -> bool | None:
    """Read ``api.enable_server`` from the newest kicad_common.json.

    Returns True/False for the pref value, or None when no readable config was
    found (KiCad not installed, or a layout we don't recognize).
    """
    root = _kicad_config_root()
    if root is None or not root.is_dir():
        return None
    for candidate in sorted(root.glob("*/kicad_common.json"), reverse=True):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        api = data.get("api")
        if isinstance(api, dict) and "enable_server" in api:
            return bool(api["enable_server"])
    return None


def connection_remedy() -> str:
    """Actionable recovery text for a refused/unanswered IPC connection.

    Encodes the restart heuristic verified live 2026-07-07: when the pref is
    already on but the server refuses, the running instance predates the pref
    (or a stale lock blocked it) — re-toggling the pref does nothing; only a
    KiCad relaunch helps.
    """
    pref = read_api_server_pref()
    if pref is False:
        return (
            "The KiCad IPC API server is disabled. In KiCad: Preferences → "
            "Plugins → enable the IPC API server, then restart KiCad with the "
            "board open."
        )
    if pref is True:
        return (
            "kicad_common.json already has api.enable_server=true but the "
            "running KiCad is not serving — it was likely started before the "
            "pref took effect. Restart KiCad with the board open (clear any "
            "stale ~*.kicad_pcb.lck next to the board first); do NOT just "
            "re-toggle the preference."
        )
    return (
        "Could not reach the KiCad IPC API server. Make sure KiCad is running "
        "with the board open and the IPC API server is enabled (Preferences → "
        "Plugins), then restart KiCad if it was enabled after launch."
    )


class IPCConnection:
    """Owns the kipy ``KiCad()`` handle and its liveness state. One per backend."""

    def __init__(self, socket_path: str | None = None, timeout_ms: int | None = None) -> None:
        self._socket_path = socket_path if socket_path is not None else _get_socket_path()
        self._timeout_ms = timeout_ms if timeout_ms is not None else _get_timeout_ms()
        self._kicad: kipy.KiCad | None = None

    @property
    def connected(self) -> bool:
        """Whether a handle is currently established (does not re-probe)."""
        return self._kicad is not None

    def connect(self) -> None:
        """Establish (or re-establish) the IPC handle and verify it with a ping.

        Raises:
            IPCUnavailableError: kipy missing, IPC disabled by env, or the
                server refused / did not answer (with remedy text, REQ-LIFE-2).
        """
        self._kicad = None
        if kipy is None:
            raise IPCUnavailableError(
                "kipy (kicad-python) is not installed; IPC board backend unavailable",
                remedy=(
                    "Install the declared dependency kicad-python "
                    "(pip install kicad-python) to enable the IPC board path; "
                    "until then board ops fall back to the SWIG bridge."
                ),
            )
        if not ipc_enabled():
            raise IPCUnavailableError(
                "IPC routing disabled by KICAD_MCP_IPC_ENABLED",
                remedy="Unset KICAD_MCP_IPC_ENABLED (or set it to 1) to re-enable the IPC board path.",
            )
        kicad = kipy.KiCad(
            socket_path=self._socket_path,
            client_name=f"kicad-mcp-{os.getpid()}",
            timeout_ms=self._timeout_ms,
        )
        try:
            # kipy 0.5.0 ships py.typed but leaves ping() unannotated
            kicad.ping()  # type: ignore[no-untyped-call]
        except Exception as exc:  # kipy ConnectionError, pynng errors, timeouts
            raise IPCUnavailableError(
                f"KiCad IPC API server unreachable: {exc}",
                remedy=connection_remedy(),
            ) from exc
        self._kicad = kicad
        logger.debug("IPC connected (socket=%s)", self._socket_path or "<kipy default>")

    def ping(self) -> bool:
        """Health probe before use (REQ-LIFE-3). Never raises.

        A failed probe drops the handle, so the next call transparently
        reconnects — this covers "connection refused" after a KiCad restart.
        """
        try:
            if self._kicad is None:
                self.connect()
            kicad = self._kicad
            if kicad is None:  # pragma: no cover — connect() sets it or raises
                return False
            # kipy 0.5.0 ships py.typed but leaves ping() unannotated
            kicad.ping()  # type: ignore[no-untyped-call]
            return True
        except Exception:
            self._kicad = None
            return False

    def board(self) -> Board:
        """Return a Board handle for the PCB document KiCad has open.

        A fresh handle is fetched on every call: the document specifier inside
        a cached Board goes stale when the user switches boards, and stale live
        handles are the exact bug class F1 exists to remove (#14). The extra
        get_open_documents round-trip is local IPC (~ms).

        Raises:
            IPCUnavailableError: server unreachable, or reachable with no PCB
                document open (which is "not available" per REQ-ROUTE-3).
        """
        if self._kicad is None:
            self.connect()
        kicad = self._kicad
        if kicad is None:  # pragma: no cover — connect() sets it or raises
            raise IPCUnavailableError("IPC connection not established")
        try:
            return kicad.get_board()
        except kipy.errors.ConnectionError as exc:
            self._kicad = None
            raise IPCUnavailableError(
                f"KiCad IPC connection dropped: {exc}",
                remedy=connection_remedy(),
            ) from exc
        except kipy.errors.ApiError as exc:
            # Server is fine — there is just no board. Keep the handle.
            raise IPCUnavailableError(
                "KiCad IPC server is reachable but no PCB document is open",
                remedy="Open the board in the KiCad PCB editor, then retry.",
            ) from exc

    def board_ready(self) -> bool:
        """True when a PCB document is open AND reports a real filename —
        i.e. loaded, not still-loading (REQ-GATE-1). Never raises."""
        try:
            return bool(self.board().name)
        except Exception:
            return False

    def is_available(self) -> bool:
        """REQ-ROUTE-3: kipy importable, routing enabled, server answering,
        and a loaded board open. A reachable server with no board open is NOT
        available — the router advances to the bridge. Never raises."""
        if kipy is None or not ipc_enabled():
            return False
        return self.ping() and self.board_ready()

    def reconnect(self) -> None:
        """Drop the stale handle and dial fresh (REQ-LIFE-3, C1b).

        After a KiCad restart the old pynng socket is dead even though the
        client still believes it is connected, so recovery is always a full
        drop + fresh connect — never a retry on the existing handle.

        Raises:
            IPCUnavailableError: the fresh connect failed (with remedy).
        """
        self._kicad = None
        self.connect()
