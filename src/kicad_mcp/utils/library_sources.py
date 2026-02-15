"""Registry of external library sources, persisted to disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("utils.library_sources")

DEFAULT_CONFIG_DIR = Path.home() / ".kicad-mcp"
DEFAULT_REGISTRY_FILE = DEFAULT_CONFIG_DIR / "library_sources.json"


class LibrarySourceRegistry:
    """Manages a persistent registry of KiCad library source directories.

    The registry is stored as a JSON file at ``~/.kicad-mcp/library_sources.json``.
    Each source has a name, filesystem path, source type (``git`` or ``local``),
    and optional origin URL.
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        self._path = registry_path or DEFAULT_REGISTRY_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._sources: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._sources = data.get("sources", {})
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load library registry %s: %s", self._path, exc)
                self._sources = {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps({"sources": self._sources}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Failed to save library registry: %s", exc)

    def register(
        self,
        name: str,
        path: str,
        source_type: str = "local",
        url: str | None = None,
    ) -> dict[str, Any]:
        """Register a library source directory."""
        entry = {
            "path": path,
            "source_type": source_type,
            "url": url,
        }
        self._sources[name] = entry
        self._save()
        return {"name": name, **entry}

    def unregister(self, name: str) -> bool:
        """Remove a source by name. Returns True if it existed."""
        if name in self._sources:
            del self._sources[name]
            self._save()
            return True
        return False

    def get(self, name: str) -> dict[str, Any] | None:
        """Get a single source entry by name."""
        entry = self._sources.get(name)
        if entry is None:
            return None
        return {"name": name, **entry}

    def list_all(self) -> list[dict[str, Any]]:
        """Return all registered sources."""
        return [{"name": k, **v} for k, v in self._sources.items()]

    def find_symbol_libs(self, source_name: str | None = None) -> list[Path]:
        """Find all .kicad_sym files across selected sources."""
        return self._find_files("*.kicad_sym", source_name)

    def find_footprint_libs(self, source_name: str | None = None) -> list[Path]:
        """Find all .pretty directories across selected sources."""
        results: list[Path] = []
        for name, entry in self._sources.items():
            if source_name and name != source_name:
                continue
            src_path = Path(entry["path"])
            if not src_path.exists():
                continue
            # A .pretty directory itself
            if src_path.suffix == ".pretty" and src_path.is_dir():
                results.append(src_path)
            else:
                results.extend(src_path.rglob("*.pretty"))
        return results

    def _find_files(self, pattern: str, source_name: str | None) -> list[Path]:
        results: list[Path] = []
        for name, entry in self._sources.items():
            if source_name and name != source_name:
                continue
            src_path = Path(entry["path"])
            if not src_path.exists():
                continue
            if src_path.is_file() and src_path.match(pattern):
                results.append(src_path)
            elif src_path.is_dir():
                results.extend(src_path.rglob(pattern))
        return results
