"""Octopart (Nexar) GraphQL ingester.

Octopart's modern API is the Nexar GraphQL endpoint. We don't fetch
symbol/footprint *files* from Nexar (that comes from SnapMagic / Ultra
Librarian), but we do query Nexar for canonical part metadata —
manufacturer, package, datasheet URL, distributor stock — and write a
metadata-only row into the parts index. That row is enough for the agent
to reason about the part during selection; if it actually wants files,
``install_part`` can be called against SnapMagic or Ultra Librarian for
the same MPN.

Authentication: requires ``NEXAR_API_TOKEN``. Tokens are issued via the
client-credentials flow at https://identity.nexar.com — this ingester
treats the token as opaque and lets the user manage refresh.
"""

from __future__ import annotations

import time
from typing import Any

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.parts_index import PartRecord
from kicad_mcp.utils.source_ingesters._http import (
    HTTPError,
    get_env_token,
    http_post_json,
)
from kicad_mcp.utils.source_ingesters.base import FetchResult, IngestResult, SourceIngester

logger = get_logger("ingester.octopart")

API_URL = "https://api.nexar.com/graphql/"

_QUERY = """
query MpnSearch($q: String!, $limit: Int!) {
  supSearchMpn(q: $q, limit: $limit) {
    results {
      part {
        mpn
        manufacturer { name }
        shortDescription
        bestDatasheet { url }
        specs {
          attribute { shortname }
          displayValue
        }
      }
    }
  }
}
"""


class OctopartIngester(SourceIngester):
    source_name = "octopart"

    def ingest(self, **kwargs: Any) -> IngestResult:
        return IngestResult(source=self.source_name, errors=[
            "Octopart is a per-part API source — bulk ingest is not supported. "
            "Use search_parts / install_part to query individual parts."
        ])

    def fetch_part(self, mpn: str, **kwargs: Any) -> FetchResult:
        token = get_env_token("NEXAR_API_TOKEN")
        if not token:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=(
                    "NEXAR_API_TOKEN environment variable is not set. "
                    "Get a token at https://portal.nexar.com/ and export "
                    "NEXAR_API_TOKEN before retrying."
                ),
            )

        try:
            payload = http_post_json(
                API_URL,
                {"query": _QUERY, "variables": {"q": mpn, "limit": 5}},
                headers={"Authorization": f"Bearer {token}"},
            )
        except HTTPError as exc:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Nexar GraphQL request failed: {exc}",
            )

        if "errors" in payload:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Nexar reported errors: {payload['errors']}",
            )

        results = (
            payload.get("data", {})
                   .get("supSearchMpn", {})
                   .get("results") or []
        )
        if not results:
            return FetchResult(
                source=self.source_name, mpn=mpn,
                message=f"Octopart returned no matches for MPN '{mpn}'.",
            )

        part = results[0].get("part") or {}
        specs = {
            s["attribute"]["shortname"]: s.get("displayValue")
            for s in part.get("specs") or []
            if s.get("attribute", {}).get("shortname")
        }

        record = PartRecord(
            source=self.source_name,
            mpn=part.get("mpn") or mpn,
            manufacturer=(part.get("manufacturer") or {}).get("name"),
            description=part.get("shortDescription"),
            package=specs.get("case_package") or specs.get("package"),
            pin_count=_safe_int(specs.get("numberofpins")),
            value=part.get("mpn") or mpn,
            symbol_lib_id=None,         # Octopart does not ship CAD files
            footprint_lib_id=None,
            symbol_path=None,
            footprint_path=None,
            datasheet_url=(part.get("bestDatasheet") or {}).get("url"),
            license="Nexar metadata terms",
            extra={"specs": specs, "fetched_at": int(time.time())},
        )
        self.index.upsert_many([record])
        return FetchResult(
            source=self.source_name, mpn=mpn,
            record=record,
            message=(
                "Indexed Octopart metadata. Octopart does not ship CAD files; "
                "call install_part with source='snapmagic' or 'ultra-librarian' "
                "to fetch the symbol/footprint."
            ),
        )


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        # Specs come back as strings like "8" or "32 (40)"; take the first int.
        s = str(v)
        digits = ""
        for c in s:
            if c.isdigit():
                digits += c
            elif digits:
                break
        return int(digits) if digits else None
    except (TypeError, ValueError):
        return None
