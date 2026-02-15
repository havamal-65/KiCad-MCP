"""Audit trail for all tool operations."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("changelog")


class ChangeLog:
    """Records all tool invocations and file modifications to a JSONL file."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        tool_name: str,
        params: dict[str, Any],
        result_status: str = "success",
        file_modified: str | None = None,
        backup_path: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record a tool invocation in the audit log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "params": _sanitize_params(params),
            "status": result_status,
        }
        if file_modified:
            entry["file_modified"] = file_modified
        if backup_path:
            entry["backup_path"] = backup_path
        if error:
            entry["error"] = error

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.error("Failed to write change log: %s", e)

    def get_recent(self, count: int = 20) -> list[dict[str, Any]]:
        """Get the most recent log entries."""
        entries: list[dict[str, Any]] = []
        if not self._log_path.exists():
            return entries

        try:
            lines = self._log_path.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-count:]:
                if line:
                    entries.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read change log: %s", e)

        return entries


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remove potentially large or sensitive data from params before logging."""
    sanitized = {}
    for key, value in params.items():
        if isinstance(value, str) and len(value) > 500:
            sanitized[key] = value[:500] + "...(truncated)"
        else:
            sanitized[key] = value
    return sanitized


def create_backup(file_path: Path, backup_dir: Path | None = None) -> Path | None:
    """Create a timestamped backup of a file before modification.

    Args:
        file_path: The file to back up.
        backup_dir: Directory for backups. Defaults to .kicad_mcp_backups/ next to the file.

    Returns:
        Path to the backup file, or None if the source doesn't exist.
    """
    if not file_path.exists():
        return None

    if backup_dir is None:
        backup_dir = file_path.parent / ".kicad_mcp_backups"

    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.stem}_{timestamp}{file_path.suffix}"
    backup_path = backup_dir / backup_name

    shutil.copy2(str(file_path), str(backup_path))
    logger.debug("Backup created: %s", backup_path)
    return backup_path
