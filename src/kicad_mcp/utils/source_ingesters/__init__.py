"""Per-source ingesters that populate the parts index.

Each ingester knows how to pull part metadata from one kind of source —
local file libraries, an HTTP API, or a per-distributor catalog — and
write normalized :class:`PartRecord` rows into a :class:`PartsIndex`.

Two flavours:

* :class:`SourceIngester.ingest` — bulk indexer, called when a source is
  registered or refreshed. For local libraries this walks the filesystem;
  for API sources it is a no-op (the API is only hit on demand).
* :class:`SourceIngester.fetch_part` — pulls a single part from the source
  on demand, downloading symbol/footprint files into the local cache and
  returning the resulting record.
"""

from __future__ import annotations

from kicad_mcp.utils.source_ingesters.base import (
    FetchResult,
    IngestResult,
    SourceIngester,
    ingester_for_source,
)
from kicad_mcp.utils.source_ingesters.local_libs import LocalLibsIngester

__all__ = [
    "FetchResult",
    "IngestResult",
    "LocalLibsIngester",
    "SourceIngester",
    "ingester_for_source",
]
