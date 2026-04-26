"""Cross-source parts catalog tools.

These tools sit *above* the per-backend library tools in
``library.py`` / ``library_manage.py``: they index symbols and footprints
from many sources (KiCad official, Digi-Key, SparkFun, WE, SnapMagic,
Ultra Librarian, Octopart, anything the user has registered) into a
single SQLite + FTS5 catalog, and expose one filterable
``search_parts`` tool plus per-part install / bootstrap helpers.

The agent-facing flow is:

    1. ``list_known_sources``        — discover what we know about
    2. ``bootstrap_known_source``    — clone or set up a source (one-time)
    3. ``index_library_source``      — build / refresh the catalog
    4. ``search_parts``              — fast multi-criteria filtering
    5. ``install_part``              — fetch from API source on demand
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.known_sources import (
    get_known_source,
    list_known_sources as catalog_list,
)
from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.parts_index import PartsIndex
from kicad_mcp.utils.response_limit import limit_response
from kicad_mcp.utils.source_ingesters import ingester_for_source

logger = get_logger("tools.parts")


def register_tools(
    mcp: FastMCP, backend: CompositeBackend, change_log: ChangeLog,
) -> None:
    """Register the parts catalog tools."""

    # One PartsIndex per server is fine — SQLite handles concurrent reads,
    # and our writes are short batches inside a single thread.
    index = PartsIndex()

    def _registry() -> LibrarySourceRegistry:
        # Re-read from disk on every call so the tool sees sources added
        # since registration (e.g. via register_library_source from
        # library_manage.py, which uses its own short-lived registry).
        return LibrarySourceRegistry()

    @mcp.tool()
    def list_known_sources() -> str:
        """List the third-party KiCad library sources this MCP can ingest.

        Returns a static catalog (Digi-Key, SparkFun, Würth Elektronik,
        KiCad official, SnapMagic, Ultra Librarian, Octopart, PCB
        Libraries Pro) with each source's kind ('git', 'api', or 'web'),
        URL, license, and the env var to set if it requires auth.

        Returns:
            JSON list of source descriptors.
        """
        sources = catalog_list()
        change_log.record("list_known_sources", {})
        return json.dumps({
            "status": "success",
            "count": len(sources),
            "sources": sources,
        }, indent=2)

    @mcp.tool()
    def bootstrap_known_source(name: str) -> str:
        """Set up a known third-party library source.

        For ``git`` sources this clones the repo (shallow) and registers
        it under the catalog name. For ``api`` sources this verifies the
        required environment variable is set so per-part fetches will
        work. For ``web`` sources this returns the homepage so the user
        can download manually.

        Args:
            name: Catalog name (e.g. 'digikey', 'sparkfun', 'snapmagic').
                  See list_known_sources for the full list.

        Returns:
            JSON describing what was done and the next step.
        """
        source = get_known_source(name)
        if source is None:
            return json.dumps({
                "status": "error",
                "error": f"Unknown source '{name}'. Call list_known_sources for valid names.",
            }, indent=2)

        change_log.record("bootstrap_known_source", {"name": name})

        if source.kind == "git":
            existing = _registry().get(source.name)
            if existing:
                return json.dumps({
                    "status": "success",
                    "name": source.name,
                    "path": existing["path"],
                    "already_registered": True,
                    "next_step": f"Run index_library_source('{source.name}') to populate the parts index.",
                }, indent=2)
            ops = backend.get_library_manage_ops()
            try:
                clone = ops.clone_library_repo(source.url, source.name, None)
            except Exception as exc:  # surface git errors cleanly
                return json.dumps({
                    "status": "error",
                    "name": source.name,
                    "error": str(exc),
                }, indent=2)
            return json.dumps({
                "status": "success",
                "name": source.name,
                "kind": "git",
                "path": clone.get("path"),
                "next_step": f"Run index_library_source('{source.name}') to populate the parts index.",
            }, indent=2)

        if source.kind == "api":
            import os
            has_key = bool(source.auth_env_var and os.environ.get(source.auth_env_var))
            return json.dumps({
                "status": "success" if has_key else "needs_auth",
                "name": source.name,
                "kind": "api",
                "auth_env_var": source.auth_env_var,
                "auth_present": has_key,
                "homepage": source.homepage,
                "next_step": (
                    f"Use search_parts(query=..., source='{source.name}') and install_part(...) "
                    "to fetch individual parts."
                    if has_key else
                    f"Set the {source.auth_env_var} environment variable, then retry."
                ),
            }, indent=2)

        # web
        return json.dumps({
            "status": "manual",
            "name": source.name,
            "kind": "web",
            "homepage": source.homepage,
            "next_step": (
                f"Download the libraries from {source.homepage}, then call "
                "register_library_source(path=<extracted dir>, name=...) to register them."
            ),
        }, indent=2)

    @mcp.tool()
    def index_library_source(source_name: str) -> str:
        """Build or refresh the parts index for a registered library source.

        Walks the source's filesystem (for git/local sources) and writes
        normalized part records into ``~/.kicad-mcp/parts_index.sqlite``.
        For API-backed sources (SnapMagic, Ultra Librarian, Octopart) this
        is a no-op — those parts only enter the index via install_part.

        Args:
            source_name: Name of a registered source. Use 'all' to index
                         every registered source.

        Returns:
            JSON summary with counts and timing.
        """
        if source_name.lower() == "all":
            results = []
            for entry in _registry().list_all():
                ingester = ingester_for_source(entry["name"], index)
                if ingester is None:
                    continue
                res = ingester.ingest()
                results.append(res.to_dict())
            change_log.record("index_library_source", {"source_name": "all"})
            return json.dumps({
                "status": "success",
                "indexed_sources": len(results),
                "results": results,
                "stats": index.stats(),
            }, indent=2)

        ingester = ingester_for_source(source_name, index)
        if ingester is None:
            return json.dumps({
                "status": "error",
                "error": f"No ingester available for source '{source_name}'.",
            }, indent=2)
        result = ingester.ingest()
        change_log.record("index_library_source", {"source_name": source_name})
        return json.dumps({
            "status": "success",
            **result.to_dict(),
            "stats": index.stats(),
        }, indent=2)

    @mcp.tool()
    def search_parts(
        query: str = "",
        source: str = "",
        package: str = "",
        manufacturer: str = "",
        min_pins: int = 0,
        max_pins: int = 0,
        limit: int = 25,
    ) -> str:
        """Search the unified parts catalog with multi-criteria filtering.

        Hits the SQLite + FTS5 index built by index_library_source, so
        results come back in milliseconds even on installs with hundreds
        of thousands of indexed parts.

        Args:
            query: Free-text search across MPN, manufacturer, description,
                   value, and lib IDs (e.g. 'STM32F4', 'op-amp rail-to-rail').
                   Omit for unfiltered listing.
            source: Restrict to a single source by name (e.g. 'digikey',
                    'kicad-symbols'). Empty = all sources.
            package: Substring match against package field (e.g. '0805',
                     'SOIC', 'QFN').
            manufacturer: Substring match against manufacturer.
            min_pins: Minimum pin count, inclusive. 0 = no lower bound.
            max_pins: Maximum pin count, inclusive. 0 = no upper bound.
            limit: Maximum results (default 25).

        Returns:
            JSON with ranked candidate rows: mpn, manufacturer, package,
            pin_count, symbol_lib_id, footprint_lib_id, datasheet_url.
        """
        results = index.search(
            query=query or None,
            source=source or None,
            package=package or None,
            manufacturer=manufacturer or None,
            min_pins=min_pins or None,
            max_pins=max_pins or None,
            limit=max(1, min(limit, 200)),
        )
        change_log.record("search_parts", {
            "query": query, "source": source, "package": package,
            "manufacturer": manufacturer,
        })
        capped = limit_response({"parts": results})
        return json.dumps({
            "status": "success",
            "query": query,
            "filters": {
                "source": source or None, "package": package or None,
                "manufacturer": manufacturer or None,
                "min_pins": min_pins or None, "max_pins": max_pins or None,
            },
            "count": len(results),
            **capped,
        }, indent=2)

    @mcp.tool()
    def install_part(mpn: str, source: str, manufacturer: str = "") -> str:
        """Fetch a part from an API-backed source and add it to the catalog.

        Used for SnapMagic, Ultra Librarian, and Octopart — the sources
        that don't ship as a single git clone. Downloads the symbol and
        footprint (when available), caches them under
        ``~/.kicad-mcp/external_libs/<source>/``, and writes a row into
        the parts index. The cached files can then be imported into a
        project library with import_symbol / import_footprint.

        Args:
            mpn: Manufacturer part number (e.g. 'STM32F407VGT6').
            source: Source name. Must be 'snapmagic', 'ultra-librarian',
                    or 'octopart'.
            manufacturer: Optional manufacturer hint to disambiguate when
                          two manufacturers ship the same MPN.

        Returns:
            JSON describing the fetched files and the index row.
        """
        ingester = ingester_for_source(source, index)
        if ingester is None:
            return json.dumps({
                "status": "error",
                "error": f"No ingester for source '{source}'.",
            }, indent=2)
        kwargs: dict[str, str] = {}
        if manufacturer:
            kwargs["manufacturer"] = manufacturer
        result = ingester.fetch_part(mpn, **kwargs)
        change_log.record("install_part", {"mpn": mpn, "source": source})
        return json.dumps({
            "status": "success" if result.record is not None else "info",
            **result.to_dict(),
        }, indent=2)

    @mcp.tool()
    def parts_index_stats() -> str:
        """Report how many parts are indexed per source.

        Useful to verify that index_library_source actually populated
        anything before relying on search_parts.

        Returns:
            JSON with total row count, per-source counts, and the index
            file path.
        """
        return json.dumps({"status": "success", **index.stats()}, indent=2)
