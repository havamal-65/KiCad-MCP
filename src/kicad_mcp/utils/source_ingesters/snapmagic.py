"""SnapMagic Search (formerly SnapEDA) ingester.

SnapMagic is a per-part fetch source: there's no bulk dataset to mirror,
so :meth:`ingest` is a no-op. Calls go through :meth:`fetch_part` which
hits the SnapMagic REST API, downloads the KiCad symbol/footprint, caches
them under ``~/.kicad-mcp/external_libs/snapmagic/``, and writes a row
into the parts index.

Authentication: requires ``SNAPMAGIC_API_KEY``. Without it ``fetch_part``
returns a clear "set the env var" message instead of raising.

The API surface used here is the documented v1 endpoint structure:
``GET /api/v1/parts/search?q=<mpn>&format=kicad`` returns a JSON envelope
with download URLs. The exact response shape is treated defensively — we
extract whatever we can and fall back to a "downloaded but unparsed"
result when fields are missing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.parts_index import PartRecord
from kicad_mcp.utils.source_ingesters._http import (
    HTTPError,
    get_env_token,
    http_download,
    http_get_json,
)
from kicad_mcp.utils.source_ingesters.base import FetchResult, IngestResult, SourceIngester

logger = get_logger("ingester.snapmagic")

API_BASE = "https://www.snapeda.com/api/v1"
CACHE_ROOT = Path.home() / ".kicad-mcp" / "external_libs" / "snapmagic"


class SnapMagicIngester(SourceIngester):
    source_name = "snapmagic"

    def ingest(self, **kwargs: Any) -> IngestResult:
        # SnapMagic is too large to bulk-mirror; ingest is a no-op so the
        # MCP tool layer can call ``ingest`` uniformly across sources.
        return IngestResult(source=self.source_name, errors=[
            "SnapMagic is a per-part API source — bulk ingest is not supported. "
            "Use search_parts / install_part to fetch individual parts on demand."
        ])

    def fetch_part(self, mpn: str, **kwargs: Any) -> FetchResult:
        token = get_env_token("SNAPMAGIC_API_KEY")
        if not token:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=(
                    "SNAPMAGIC_API_KEY environment variable is not set. "
                    "Get an API key from https://www.snapeda.com/account/ "
                    "and export SNAPMAGIC_API_KEY before retrying."
                ),
            )

        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        manufacturer = kwargs.get("manufacturer") or ""
        try:
            payload = http_get_json(
                f"{API_BASE}/parts/search",
                params={"q": mpn, "manufacturer": manufacturer, "format": "kicad"},
                headers={"Authorization": f"Token {token}"},
            )
        except HTTPError as exc:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"SnapMagic search failed: {exc}",
            )

        results = payload.get("results") or payload.get("parts") or []
        if not results:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"SnapMagic returned no matches for MPN '{mpn}'.",
            )

        part = results[0]
        sym_url = _first_url(part, ("symbol_url", "symbol_kicad_url", "symbol_download_url"))
        fp_url = _first_url(part, ("footprint_url", "footprint_kicad_url", "footprint_download_url"))

        sym_path: Path | None = None
        fp_path: Path | None = None

        if sym_url:
            sym_path = CACHE_ROOT / "symbols" / f"{_safe(mpn)}.kicad_sym"
            sym_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                http_download(sym_url, str(sym_path), headers={"Authorization": f"Token {token}"})
            except HTTPError as exc:
                logger.warning("Symbol download failed for %s: %s", mpn, exc)
                sym_path = None

        if fp_url:
            fp_dir = CACHE_ROOT / f"{_safe(mpn)}.pretty"
            fp_dir.mkdir(parents=True, exist_ok=True)
            try:
                http_download(fp_url, str(fp_dir / f"{_safe(mpn)}.kicad_mod"))
            except HTTPError as exc:
                logger.warning("Footprint download failed for %s: %s", mpn, exc)
                fp_dir = None  # type: ignore[assignment]
            else:
                fp_path = fp_dir

        record = PartRecord(
            source=self.source_name,
            mpn=part.get("mpn") or mpn,
            manufacturer=part.get("manufacturer"),
            description=part.get("description") or part.get("short_description"),
            package=part.get("package") or part.get("case_package"),
            pin_count=_safe_int(part.get("pin_count")),
            value=part.get("mpn") or mpn,
            symbol_lib_id=f"snapmagic_{_safe(mpn)}:{_safe(mpn)}" if sym_path else None,
            footprint_lib_id=f"snapmagic_{_safe(mpn)}:{_safe(mpn)}" if fp_path else None,
            symbol_path=str(sym_path) if sym_path else None,
            footprint_path=str(fp_path) if fp_path else None,
            datasheet_url=part.get("datasheet_url"),
            license=part.get("license") or "SnapMagic terms",
            extra={"snapmagic_id": part.get("id"), "fetched_at": int(time.time())},
        )
        self.index.upsert_many([record])
        return FetchResult(
            source=self.source_name, mpn=mpn,
            record=record,
            symbol_path=sym_path, footprint_path=fp_path,
            message="Fetched and cached.",
        )


def _first_url(part: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = part.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
