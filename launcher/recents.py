"""Recent-projects store and project discovery — pure, import-safe (no GUI).

Recents live in a small JSON list at `cfg.recents_path`, most-recent first.
Reads are tolerant (corrupt/missing → empty); writes are atomic (temp file +
`os.replace`). `list_for_picker` unions existing recents with a filesystem scan
of the configured project roots, most-recent first, dropping recents whose file
no longer exists (REQ-PROJ-*).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from launcher.config import LauncherConfig


@dataclass(frozen=True)
class RecentEntry:
    path: str
    name: str
    last_used: float


@dataclass(frozen=True)
class PickerItem:
    path: Path
    name: str
    exists: bool
    last_used: float | None


def load_recents(cfg: LauncherConfig) -> list[RecentEntry]:
    """Load recents most-recent first. Corrupt/missing file → []."""
    try:
        raw = cfg.recents_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[RecentEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = Path(path).stem
        last_used = item.get("last_used")
        if not isinstance(last_used, (int, float)):
            last_used = 0.0
        entries.append(RecentEntry(path=path, name=name, last_used=float(last_used)))
    return entries


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def promote(cfg: LauncherConfig, board_path: Path) -> None:
    """Move `board_path` to the front of recents with a fresh timestamp."""
    board_path = Path(board_path)
    key = str(board_path.resolve()) if board_path.exists() else str(board_path)
    existing = [
        e
        for e in load_recents(cfg)
        if _norm(e.path) != _norm(key)
    ]
    head = RecentEntry(path=key, name=board_path.stem, last_used=time.time())
    ordered = [head] + existing
    payload = [
        {"path": e.path, "name": e.name, "last_used": e.last_used} for e in ordered
    ]
    _atomic_write(cfg.recents_path, json.dumps(payload, indent=2))


def _norm(p: str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return str(Path(p))


def resolve_board_path(picked: Path | str) -> Path | None:
    """Resolve a user-browsed file to a board: a `.kicad_pcb` is itself; a
    `.kicad_pro` resolves to its sibling `.kicad_pcb`. None if unresolvable."""
    p = Path(picked)
    suffix = p.suffix.lower()
    if suffix == ".kicad_pcb":
        return p if p.exists() else None
    if suffix == ".kicad_pro":
        sibling = p.with_suffix(".kicad_pcb")
        return sibling if sibling.exists() else None
    return None


def discover_projects(roots: list[Path]) -> list[Path]:
    """Recursively find *.kicad_pcb under each root. De-duplicated, sorted."""
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for pcb in root.rglob("*.kicad_pcb"):
            # Skip auto-backups: KiCad's <project>-backups dirs and the MCP's
            # own .kicad_mcp_backups snapshots.
            if any(
                part.endswith("-backups") or part == ".kicad_mcp_backups"
                for part in pcb.parts
            ):
                continue
            found[_norm(str(pcb))] = pcb
    return [found[k] for k in sorted(found)]


def list_for_picker(cfg: LauncherConfig) -> list[PickerItem]:
    """Existing recents (most-recent first) then discovered projects not already
    listed. Recents whose file is gone are dropped (pruned)."""
    items: list[PickerItem] = []
    seen: set[str] = set()

    for e in load_recents(cfg):
        p = Path(e.path)
        if not p.exists():
            continue  # prune missing
        key = _norm(e.path)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            PickerItem(path=p, name=e.name, exists=True, last_used=e.last_used)
        )

    for pcb in discover_projects(cfg.projects_roots):
        key = _norm(str(pcb))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            PickerItem(path=pcb, name=pcb.stem, exists=True, last_used=None)
        )

    return items
