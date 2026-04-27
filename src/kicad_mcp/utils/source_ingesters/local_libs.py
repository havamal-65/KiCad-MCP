"""Bulk ingester for any source that ships ``.kicad_sym`` / ``.pretty`` files.

Used for KiCad's official libraries, Digi-Key, SparkFun, Würth Elektronik,
project-local libraries, and any custom directory the user registers.

The ingester reads files with regex (not full s-expression parsing) so it
stays fast on installs with hundreds of MB of libraries. The metadata it
extracts is intentionally a subset of what KiCad stores — just enough for
the agent to filter, rank, and pick a part.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.parts_index import PartRecord, PartsIndex
from kicad_mcp.utils.source_ingesters.base import IngestResult, SourceIngester

logger = get_logger("ingester.local")


# (symbol "Name" — top-level only (excludes sub-units like "R/0")
_SYM_HEADER = re.compile(r'^\t\(symbol\s+"([^"/]+)"', re.MULTILINE)
_PROP_PATTERN = re.compile(r'\(property\s+"([^"]+)"\s+"([^"]*)"', re.DOTALL)
_PIN_PATTERN = re.compile(r'\(pin\s+\w+\s+\w+\s*\n')

# Footprint file: (footprint "Name", and (descr "..."), (tags "..."), (pad ...)
_FP_HEADER = re.compile(r'\(footprint\s+"([^"]+)"')
_FP_DESCR = re.compile(r'\(descr\s+"([^"]*)"')
_FP_TAGS = re.compile(r'\(tags\s+"([^"]*)"')
_FP_PAD = re.compile(r'\(pad\s+"?([^"\s)]+)"?\s+\w+\s+\w+')


# Properties that, when present, give us part-level identity. Different
# sources use slightly different keys, so accept a small set.
_MPN_KEYS = {
    "mpn", "manufacturer_part_number", "manufacturerpartnumber",
    "part number", "part_number", "partnumber",
}
_MFR_KEYS = {"manufacturer", "manufacturer_name", "mfr"}
_PACKAGE_KEYS = {"package", "case", "case/package", "case_package"}
_DATASHEET_KEYS = {"datasheet"}


class LocalLibsIngester(SourceIngester):
    """Walks a registered library source root and indexes its parts."""

    def __init__(
        self,
        index: PartsIndex,
        source_name: str,
        registry: LibrarySourceRegistry | None = None,
    ) -> None:
        super().__init__(index)
        self.source_name = source_name
        self._registry = registry or LibrarySourceRegistry()

    def ingest(self, **kwargs: Any) -> IngestResult:
        start = time.monotonic()
        result = IngestResult(source=self.source_name)

        symbol_files = self._registry.find_symbol_libs(self.source_name)
        footprint_dirs = self._registry.find_footprint_libs(self.source_name)

        if not symbol_files and not footprint_dirs:
            result.errors.append(
                f"No .kicad_sym or .pretty paths found under source '{self.source_name}'. "
                "Register the source first or check that it points at a real directory."
            )
            result.duration_s = time.monotonic() - start
            return result

        # Refresh: drop existing rows for this source so renamed/removed
        # parts don't linger.
        self.index.delete_source(self.source_name)

        records: list[PartRecord] = []
        for sym_path in symbol_files:
            try:
                records.extend(self._index_symbol_file(sym_path))
            except Exception as exc:
                result.errors.append(f"{sym_path}: {exc}")
                result.skipped += 1

        for fp_dir in footprint_dirs:
            try:
                records.extend(self._index_footprint_dir(fp_dir))
            except Exception as exc:
                result.errors.append(f"{fp_dir}: {exc}")
                result.skipped += 1

        if records:
            # Push in batches so we don't hold one giant transaction.
            batch = 500
            for i in range(0, len(records), batch):
                self.index.upsert_many(records[i:i + batch])
            result.indexed = len(records)

        result.duration_s = time.monotonic() - start
        logger.info(
            "Indexed source=%s symbols+footprints=%d errors=%d in %.2fs",
            self.source_name, result.indexed, len(result.errors), result.duration_s,
        )
        return result

    # -- per-file workers ----------------------------------------------------

    def _index_symbol_file(self, sym_path: Path) -> list[PartRecord]:
        text = sym_path.read_text(encoding="utf-8", errors="ignore")
        lib_name = sym_path.stem
        out: list[PartRecord] = []

        for sym_match in _SYM_HEADER.finditer(text):
            sym_name = sym_match.group(1)
            block = _extract_symbol_block(text, sym_match.start())
            if block is None:
                continue
            props = _parse_properties(block)
            pin_count = _count_pins(block)

            mpn = _pick_first(props, _MPN_KEYS) or sym_name
            manufacturer = _pick_first(props, _MFR_KEYS)
            package = _pick_first(props, _PACKAGE_KEYS)
            description = props.get("description")
            value = props.get("value", sym_name)
            datasheet = _pick_first(props, _DATASHEET_KEYS)
            footprint = props.get("footprint", "").strip() or None

            out.append(PartRecord(
                source=self.source_name,
                mpn=mpn,
                manufacturer=manufacturer,
                description=description,
                package=package,
                pin_count=pin_count,
                value=value,
                symbol_lib_id=f"{lib_name}:{sym_name}",
                footprint_lib_id=footprint if footprint and ":" in footprint else None,
                symbol_path=str(sym_path),
                footprint_path=None,
                datasheet_url=datasheet if datasheet and datasheet != "~" else None,
                license=None,
                extra={
                    "fp_filters": props.get("ki_fp_filters", "").split() if props.get("ki_fp_filters") else [],
                    "keywords": props.get("ki_keywords", ""),
                },
            ))
        return out

    def _index_footprint_dir(self, fp_dir: Path) -> list[PartRecord]:
        lib_name = fp_dir.stem.replace(".pretty", "")
        out: list[PartRecord] = []
        for fp_file in fp_dir.glob("*.kicad_mod"):
            try:
                text = fp_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            header = _FP_HEADER.search(text)
            if header is None:
                continue
            fp_name = header.group(1)
            descr = (_FP_DESCR.search(text) or [None, None])[1] if _FP_DESCR.search(text) else None
            tags = (_FP_TAGS.search(text) or [None, None])[1] if _FP_TAGS.search(text) else None
            # Distinct pad numbers — pads can be repeated for thermal vias etc.
            pads = {m.group(1) for m in _FP_PAD.finditer(text)}

            out.append(PartRecord(
                source=self.source_name,
                mpn=fp_name,
                manufacturer=None,
                description=descr,
                package=_guess_package_from_name(fp_name),
                pin_count=len(pads) if pads else None,
                value=fp_name,
                symbol_lib_id=None,
                footprint_lib_id=f"{lib_name}:{fp_name}",
                symbol_path=None,
                footprint_path=str(fp_dir),
                datasheet_url=None,
                license=None,
                extra={"tags": tags or ""},
            ))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_symbol_block(text: str, start: int) -> str | None:
    """Return the balanced ``(symbol ...)`` block beginning near *start*.

    *start* is the offset of the leading tab returned by ``_SYM_HEADER``.
    The block is needed because property regex extraction must stay scoped
    to one symbol — otherwise properties from the next symbol leak in.
    """
    paren = text.find("(", start)
    if paren < 0:
        return None
    depth = 0
    i = paren
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[paren:i + 1]
        i += 1
    return None


def _parse_properties(block: str) -> dict[str, str]:
    """Pull ``(property "Name" "Value" ...)`` pairs out of a symbol block.

    Returns a dict keyed by lowercased property name. Sub-unit symbols inside
    the block also have properties that look like this, but their fields
    (Reference, Value...) match the parent's so the overwrite is benign.
    """
    out: dict[str, str] = {}
    for m in _PROP_PATTERN.finditer(block):
        out[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def _count_pins(block: str) -> int | None:
    """Count ``(pin ...)`` entries in a symbol block.

    Returns None if the symbol has no pin definitions (e.g. graphic-only
    power flags).
    """
    n = len(_PIN_PATTERN.findall(block))
    return n if n else None


def _pick_first(props: dict[str, str], keys: set[str]) -> str | None:
    for k in keys:
        if k in props and props[k]:
            return props[k]
    return None


_PACKAGE_NAME_HINTS = (
    "0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512",
    "SOIC", "SOT", "SOP", "TSSOP", "QFN", "QFP", "BGA", "DIP", "TO-",
    "DFN", "MSOP", "LGA", "WLCSP", "SMA", "SMB", "SMC",
)


def _guess_package_from_name(fp_name: str) -> str | None:
    """Cheap heuristic — look for common package tokens in the footprint name.

    Used so footprint-only rows are still filterable by package even when
    the .kicad_mod file has no explicit package property.
    """
    upper = fp_name.upper()
    for hint in _PACKAGE_NAME_HINTS:
        if hint in upper:
            return hint
    return None
