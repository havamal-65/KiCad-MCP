"""Abstract base for per-source ingesters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_mcp.utils.parts_index import PartRecord, PartsIndex


@dataclass
class IngestResult:
    """Summary returned from a bulk ingest pass."""

    source: str
    indexed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "errors": self.errors[:10],  # cap so a runaway loop doesn't bloat output
            "error_count": len(self.errors),
            "duration_s": round(self.duration_s, 2),
        }


@dataclass
class FetchResult:
    """Returned from :meth:`SourceIngester.fetch_part`."""

    source: str
    mpn: str
    record: PartRecord | None = None
    symbol_path: Path | None = None
    footprint_path: Path | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "mpn": self.mpn,
            "symbol_path": str(self.symbol_path) if self.symbol_path else None,
            "footprint_path": str(self.footprint_path) if self.footprint_path else None,
            "message": self.message,
            "indexed": self.record is not None,
        }


class SourceIngester(ABC):
    """Interface for one library source."""

    #: Catalog name (matches ``KnownSource.name``). Set by subclass.
    source_name: str = ""

    def __init__(self, index: PartsIndex) -> None:
        self.index = index

    @abstractmethod
    def ingest(self, **kwargs: Any) -> IngestResult:
        """Bulk-index everything this source currently exposes locally."""

    def fetch_part(self, mpn: str, **kwargs: Any) -> FetchResult:
        """Resolve and download a single part by MPN.

        Default implementation returns a "not supported" result. API-backed
        ingesters override this; bulk-only ingesters (local libs) leave it
        alone because their parts are already indexed.
        """
        return FetchResult(
            source=self.source_name,
            mpn=mpn,
            message=(
                f"Source '{self.source_name}' does not support per-part fetch; "
                "the source must be ingested in bulk."
            ),
        )


def ingester_for_source(source_name: str, index: PartsIndex) -> SourceIngester | None:
    """Construct the right ingester for *source_name*, or None if unknown."""
    # Imported here to keep the module import cheap.
    from kicad_mcp.utils.source_ingesters.local_libs import LocalLibsIngester
    from kicad_mcp.utils.source_ingesters.octopart import OctopartIngester
    from kicad_mcp.utils.source_ingesters.snapmagic import SnapMagicIngester
    from kicad_mcp.utils.source_ingesters.ultra_librarian import UltraLibrarianIngester

    name = source_name.lower()
    if name == "snapmagic":
        return SnapMagicIngester(index)
    if name == "ultra-librarian":
        return UltraLibrarianIngester(index)
    if name == "octopart":
        return OctopartIngester(index)
    # All other sources (KiCad official, Digi-Key, SparkFun, WE, project
    # libraries, anything registered as a local/git path) are handled
    # uniformly by the local-libs ingester pointed at the source root.
    return LocalLibsIngester(index, source_name=source_name)
