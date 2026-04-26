"""SQLite-backed index of KiCad parts across all registered library sources.

The index gives the MCP a single, fast searchable surface over symbols and
footprints from many sources (KiCad official, Digi-Key, SparkFun, WE,
SnapMagic, Ultra Librarian, Octopart, project-local libraries).

Schema
------

``parts``
    One row per indexed *part*. A part is the combination of a symbol and
    (when known) a recommended footprint. Sources that ship symbol-only or
    footprint-only entries still get a row — the missing side is NULL.

``parts_fts``
    SQLite FTS5 virtual table mirroring the searchable text columns of
    ``parts``. Full-text queries hit this table; structured filters
    (``package``, ``manufacturer``, pin count, source) hit ``parts``.

The index lives at ``~/.kicad-mcp/parts_index.sqlite`` by default. It is
fully rebuildable from the underlying library files / API caches; deleting
it costs only a re-index.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger

logger = get_logger("utils.parts_index")

DEFAULT_INDEX_PATH = Path.home() / ".kicad-mcp" / "parts_index.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    mpn             TEXT,
    manufacturer    TEXT,
    description     TEXT,
    package         TEXT,
    pin_count       INTEGER,
    value           TEXT,
    symbol_lib_id   TEXT,           -- "Library:Symbol"
    footprint_lib_id TEXT,          -- "Library:Footprint"
    symbol_path     TEXT,           -- absolute .kicad_sym path
    footprint_path  TEXT,           -- absolute .pretty dir
    datasheet_url   TEXT,
    license         TEXT,
    extra_json      TEXT,           -- JSON blob for source-specific fields
    indexed_at      INTEGER NOT NULL,
    UNIQUE(source, symbol_lib_id, footprint_lib_id, mpn)
);

CREATE INDEX IF NOT EXISTS parts_source_idx       ON parts(source);
CREATE INDEX IF NOT EXISTS parts_package_idx      ON parts(package);
CREATE INDEX IF NOT EXISTS parts_manufacturer_idx ON parts(manufacturer);
CREATE INDEX IF NOT EXISTS parts_mpn_idx          ON parts(mpn);
CREATE INDEX IF NOT EXISTS parts_pincount_idx     ON parts(pin_count);

CREATE VIRTUAL TABLE IF NOT EXISTS parts_fts USING fts5(
    mpn, manufacturer, description, value,
    symbol_lib_id, footprint_lib_id,
    content='parts', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS parts_ai AFTER INSERT ON parts BEGIN
    INSERT INTO parts_fts(rowid, mpn, manufacturer, description, value,
                          symbol_lib_id, footprint_lib_id)
    VALUES (new.id, new.mpn, new.manufacturer, new.description, new.value,
            new.symbol_lib_id, new.footprint_lib_id);
END;

CREATE TRIGGER IF NOT EXISTS parts_ad AFTER DELETE ON parts BEGIN
    INSERT INTO parts_fts(parts_fts, rowid, mpn, manufacturer, description, value,
                          symbol_lib_id, footprint_lib_id)
    VALUES('delete', old.id, old.mpn, old.manufacturer, old.description, old.value,
           old.symbol_lib_id, old.footprint_lib_id);
END;
"""


@dataclass
class PartRecord:
    """One row in the parts index."""

    source: str
    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    package: str | None = None
    pin_count: int | None = None
    value: str | None = None
    symbol_lib_id: str | None = None
    footprint_lib_id: str | None = None
    symbol_path: str | None = None
    footprint_path: str | None = None
    datasheet_url: str | None = None
    license: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PartsIndex:
    """Thin wrapper around a SQLite + FTS5 parts database."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_INDEX_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes --------------------------------------------------------------

    def upsert_many(self, records: list[PartRecord]) -> int:
        """Insert or refresh records. Returns the number of rows touched."""
        if not records:
            return 0
        import json as _json

        now = int(time.time())
        rows = [
            (
                r.source,
                r.mpn,
                r.manufacturer,
                r.description,
                r.package,
                r.pin_count,
                r.value,
                r.symbol_lib_id,
                r.footprint_lib_id,
                r.symbol_path,
                r.footprint_path,
                r.datasheet_url,
                r.license,
                _json.dumps(r.extra) if r.extra else None,
                now,
            )
            for r in records
        ]
        cur = self._conn.executemany(
            """
            INSERT INTO parts (source, mpn, manufacturer, description, package,
                               pin_count, value, symbol_lib_id, footprint_lib_id,
                               symbol_path, footprint_path, datasheet_url,
                               license, extra_json, indexed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, symbol_lib_id, footprint_lib_id, mpn) DO UPDATE SET
                manufacturer=excluded.manufacturer,
                description=excluded.description,
                package=excluded.package,
                pin_count=excluded.pin_count,
                value=excluded.value,
                symbol_path=excluded.symbol_path,
                footprint_path=excluded.footprint_path,
                datasheet_url=excluded.datasheet_url,
                license=excluded.license,
                extra_json=excluded.extra_json,
                indexed_at=excluded.indexed_at
            """,
            rows,
        )
        self._conn.commit()
        return cur.rowcount or len(rows)

    def delete_source(self, source: str) -> int:
        """Drop every row contributed by *source* (used before a re-index)."""
        cur = self._conn.execute("DELETE FROM parts WHERE source = ?", (source,))
        self._conn.commit()
        return cur.rowcount

    # -- reads ---------------------------------------------------------------

    def search(
        self,
        query: str | None = None,
        *,
        source: str | None = None,
        package: str | None = None,
        manufacturer: str | None = None,
        min_pins: int | None = None,
        max_pins: int | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Filter the index. Returns up to *limit* rows ranked by FTS score."""
        clauses: list[str] = []
        params: list[Any] = []
        join_fts = False

        if query:
            join_fts = True
            clauses.append("parts.id = parts_fts.rowid")
            clauses.append("parts_fts MATCH ?")
            params.append(_format_fts_query(query))
        if source:
            clauses.append("parts.source = ?")
            params.append(source)
        if package:
            clauses.append("LOWER(parts.package) LIKE ?")
            params.append(f"%{package.lower()}%")
        if manufacturer:
            clauses.append("LOWER(parts.manufacturer) LIKE ?")
            params.append(f"%{manufacturer.lower()}%")
        if min_pins is not None:
            clauses.append("parts.pin_count >= ?")
            params.append(min_pins)
        if max_pins is not None:
            clauses.append("parts.pin_count <= ?")
            params.append(max_pins)

        sql = "SELECT parts.* FROM parts"
        if join_fts:
            sql += ", parts_fts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if join_fts:
            sql += " ORDER BY bm25(parts_fts)"
        else:
            sql += " ORDER BY parts.indexed_at DESC"
        sql += " LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_by_mpn(self, mpn: str, source: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM parts WHERE LOWER(mpn) = ?"
        params: list[Any] = [mpn.lower()]
        if source:
            sql += " AND source = ?"
            params.append(source)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """Per-source row counts and totals."""
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS n FROM parts GROUP BY source"
        ).fetchall()
        per_source = {r["source"]: r["n"] for r in rows}
        total = sum(per_source.values())
        return {"total": total, "per_source": per_source, "path": str(self._path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FTS_SPECIAL = set('"\'()[]{}*:')


def _format_fts_query(q: str) -> str:
    """Make a free-text query safe for FTS5 MATCH.

    Strips characters that FTS5 treats as operators, then wraps each
    surviving token in a prefix match. Empty/short tokens are dropped.
    """
    cleaned: list[str] = []
    for raw in q.split():
        token = "".join(c for c in raw if c not in _FTS_SPECIAL)
        token = token.strip()
        if len(token) >= 2:
            cleaned.append(f'"{token}"*')
    if not cleaned:
        return '""'
    return " ".join(cleaned)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    import json as _json

    d = dict(row)
    extra = d.pop("extra_json", None)
    if extra:
        try:
            d["extra"] = _json.loads(extra)
        except _json.JSONDecodeError:
            d["extra"] = {}
    else:
        d["extra"] = {}
    return d
