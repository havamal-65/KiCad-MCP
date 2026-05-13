"""Sidecar validation cache for tool-level gate enforcement (Phase 6.1.4).

The cache lives next to each ``.kicad_pcb`` as ``<board>.validation_cache.json``.
It records the SHA-256 of the board content at the time each validator ran,
so that downstream gates (e.g. ``autoroute``) can verify a prior validation
result is still applicable to the current board state.

When the board hash changes, the entire cache is invalidated and re-created on
the next write — any prior validator results no longer reflect the live board.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("utils.validation_cache")


def _cache_path(pcb_path: Path) -> Path:
    return pcb_path.with_suffix(pcb_path.suffix + ".validation_cache.json")


def compute_board_hash(pcb_path: Path) -> str:
    """Hex SHA-256 of the .kicad_pcb file content.

    Used as the cache invalidation key — any byte-level change to the board
    file (move, autoroute, edit) produces a different hash and invalidates
    every prior validator entry.
    """
    return hashlib.sha256(pcb_path.read_bytes()).hexdigest()


def _read_cache(pcb_path: Path) -> dict[str, Any] | None:
    cache_file = _cache_path(pcb_path)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Corrupt validation cache at %s: %s — ignoring", cache_file, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def record_validation(
    pcb_path: Path, validator_name: str, result: dict[str, Any]
) -> None:
    """Persist a validator result to the sidecar cache.

    If the board hash has changed since the last write, the existing cache is
    discarded — stale validator results would otherwise pass through with an
    out-of-date board, which is exactly the bug this cache exists to prevent.

    Args:
        pcb_path: Path to the .kicad_pcb file the validator was run on.
        validator_name: Stable identifier (e.g. "validate_connector_orientations").
        result: The full validator result dict — written verbatim under the
            ``validators[<name>]`` key alongside a UTC timestamp.
    """
    current_hash = compute_board_hash(pcb_path)
    existing = _read_cache(pcb_path)

    if existing is None or existing.get("board_sha256") != current_hash:
        cache: dict[str, Any] = {"board_sha256": current_hash, "validators": {}}
    else:
        cache = existing

    entry = dict(result)
    entry["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cache.setdefault("validators", {})[validator_name] = entry

    _cache_path(pcb_path).write_text(
        json.dumps(cache, indent=2), encoding="utf-8"
    )


def get_validation(
    pcb_path: Path, validator_name: str
) -> dict[str, Any] | None:
    """Read a cached validator result if it applies to the current board.

    Returns ``None`` if the cache is missing, corrupt, hashes a different
    board version, or has no entry for *validator_name*. Returns the stored
    result dict otherwise. A non-None return is the cache's promise that this
    validator ran against the board's current byte content.
    """
    data = _read_cache(pcb_path)
    if data is None:
        return None
    if data.get("board_sha256") != compute_board_hash(pcb_path):
        return None
    validators = data.get("validators", {})
    if not isinstance(validators, dict):
        return None
    entry = validators.get(validator_name)
    if not isinstance(entry, dict):
        return None
    return entry
