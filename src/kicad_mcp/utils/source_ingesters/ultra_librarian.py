"""Ultra Librarian per-part fetch ingester.

Ultra Librarian publishes a partner CAD download API. As with SnapMagic,
there's no public bulk dataset to mirror — :meth:`ingest` is a no-op and
parts come in one MPN at a time via :meth:`fetch_part`.

Authentication: requires ``ULTRA_LIBRARIAN_API_KEY`` (and optionally
``ULTRA_LIBRARIAN_USERNAME``). The endpoint structure here matches
Ultra Librarian's documented "search by MPN → request KiCad export →
download zip" flow; the JSON shape is treated defensively.
"""

from __future__ import annotations

import time
import zipfile
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

logger = get_logger("ingester.ultralibrarian")

API_BASE = "https://app.ultralibrarian.com/api"
CACHE_ROOT = Path.home() / ".kicad-mcp" / "external_libs" / "ultra-librarian"


class UltraLibrarianIngester(SourceIngester):
    source_name = "ultra-librarian"

    def ingest(self, **kwargs: Any) -> IngestResult:
        return IngestResult(source=self.source_name, errors=[
            "Ultra Librarian is a per-part API source — bulk ingest is not supported. "
            "Use search_parts / install_part to fetch individual parts on demand."
        ])

    def fetch_part(self, mpn: str, **kwargs: Any) -> FetchResult:
        token = get_env_token("ULTRA_LIBRARIAN_API_KEY")
        if not token:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=(
                    "ULTRA_LIBRARIAN_API_KEY environment variable is not set. "
                    "Request an API key at https://www.ultralibrarian.com/api "
                    "and export ULTRA_LIBRARIAN_API_KEY before retrying."
                ),
            )

        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            search = http_get_json(
                f"{API_BASE}/v1/search",
                params={"query": mpn, "format": "kicad"},
                headers={"X-Api-Key": token},
            )
        except HTTPError as exc:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Ultra Librarian search failed: {exc}",
            )

        candidates = search.get("results") or search.get("parts") or []
        if not candidates:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Ultra Librarian returned no matches for MPN '{mpn}'.",
            )

        part = candidates[0]
        download_url = part.get("download_url") or part.get("kicad_zip_url")
        if not download_url:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Ultra Librarian result for '{mpn}' has no KiCad download URL.",
            )

        zip_path = CACHE_ROOT / f"{_safe(mpn)}.zip"
        try:
            http_download(download_url, str(zip_path), headers={"X-Api-Key": token})
        except HTTPError as exc:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Ultra Librarian zip download failed: {exc}",
            )

        extract_dir = CACHE_ROOT / _safe(mpn)
        extract_dir.mkdir(parents=True, exist_ok=True)
        sym_path, fp_path = _extract_kicad_artifacts(zip_path, extract_dir)

        record = PartRecord(
            source=self.source_name,
            mpn=part.get("mpn") or mpn,
            manufacturer=part.get("manufacturer"),
            description=part.get("description"),
            package=part.get("package"),
            pin_count=_safe_int(part.get("pin_count")),
            value=part.get("mpn") or mpn,
            symbol_lib_id=f"ul_{_safe(mpn)}:{_safe(mpn)}" if sym_path else None,
            footprint_lib_id=f"ul_{_safe(mpn)}:{_safe(mpn)}" if fp_path else None,
            symbol_path=str(sym_path) if sym_path else None,
            footprint_path=str(fp_path) if fp_path else None,
            datasheet_url=part.get("datasheet_url"),
            license="Ultra Librarian terms",
            extra={"ul_id": part.get("id"), "fetched_at": int(time.time())},
        )
        self.index.upsert_many([record])
        return FetchResult(
            source=self.source_name, mpn=mpn,
            record=record,
            symbol_path=sym_path, footprint_path=fp_path,
            message="Fetched, extracted, and cached.",
        )


def _extract_kicad_artifacts(zip_path: Path, dest: Path) -> tuple[Path | None, Path | None]:
    """Unzip and return (first .kicad_sym, first .pretty dir) found inside.

    Ultra Librarian zips bundle several CAD formats; we only want the KiCad
    artifacts.
    """
    sym: Path | None = None
    pretty: Path | None = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile:
        return None, None

    for p in dest.rglob("*.kicad_sym"):
        sym = p
        break
    for p in dest.rglob("*.pretty"):
        if p.is_dir():
            pretty = p
            break
    return sym, pretty


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
