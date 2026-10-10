"""K10-S4 variant authoring — file-level readers/writers + write session.

KiCad 10 stores design variants in three places (grammar ground-truthed from
KiCad 10 source during K10-S3, REQ-S3B-000):

- ``.kicad_pro`` — the registry: ``schematic.variants: [{name, description?}]``
- ``.kicad_sch`` (format >= 20260306) — per-symbol, inside
  ``(instances (project … (path … (reference…) (unit…))))``:
  ``(variant (name "X") (dnp yes)? (in_bom …)? (in_pos_files …)?
  (exclude_from_sim …)? (field (name…) (value…))*)`` — members are omitted
  when equal to the symbol's base attribute; ``in_bom``/``in_pos_files`` are
  positive logic.
- ``.kicad_pcb`` (format >= 20260206) — board-level
  ``(variants (variant (name…) (description…)?)…)`` after the setup section,
  plus per-footprint ``(variant (name…) (dnp…)? (exclude_from_bom…)?
  (exclude_from_pos_files…)? (field…)*)`` — note the *negative* logic on this
  side; the writers here do the inversion so callers only ever see the
  positive ``in_bom``/``in_pos_files`` form.

All writers are pure ``str -> str`` (REQ-S4B-007: untouched bytes preserved
verbatim); file I/O and oracle validation live in :class:`VariantWriteSession`
(REQ-S4B-006/009: write-validate-commit, atomic, stale-guarded).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.sexp_parser import (  # noqa: PLC2701 — shared s-expr primitives
    _unescape_sexp_string,
    _walk_balanced_parens,
    find_footprint_block_by_reference,
)

logger = get_logger("variant_files")

# Minimum on-disk format stamps that carry variant tokens (REQ-S4B-008).
SCH_MIN_FORMAT = 20260306
PCB_MIN_FORMAT = 20260206

_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")
# KiCad-quoted string: handles escaped quotes/backslashes.
_QSTR = r'"((?:[^"\\]|\\.)*)"'
_NAME_RE = re.compile(rf"\(name\s+{_QSTR}")
_VALUE_RE = re.compile(rf"\(value\s+{_QSTR}")
_PROP_RE = re.compile(rf"\(property\s+{_QSTR}\s+{_QSTR}")

# Ordered member tokens a variant block may carry (schematic / board side).
_SCH_BOOL_TOKENS = ("dnp", "in_bom", "in_pos_files", "exclude_from_sim")
_PCB_BOOL_TOKENS = ("dnp", "exclude_from_bom", "exclude_from_pos_files")


class VariantAuthorError(Exception):
    """A variant authoring request that cannot be satisfied.

    ``payload`` is the structured detail the tool layer returns as its JSON
    error body (merged over ``{"status": "error"}``).
    """

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = payload or {
            "error": "parameter_error",
            "message": message,
        }


@dataclass
class VariantOverrides:
    """Positive-logic per-component overrides as the tool layer exposes them.

    ``None`` = leave any existing override member untouched. A non-None value
    equal to the component's base attribute *removes* the member (KiCad's
    omit-when-equal-to-base convention). ``fields`` maps field name to the
    per-variant value; a value equal to the base field value removes that
    field override.
    """

    dnp: bool | None = None
    in_bom: bool | None = None
    in_pos_files: bool | None = None
    exclude_from_sim: bool | None = None
    fields: dict[str, str] = field(default_factory=dict)


# ── shared s-expression helpers ──────────────────────────────────────────────

def file_format_version(text: str) -> int | None:
    """The file's ``(version NNNN)`` stamp, or None if absent."""
    m = _VERSION_RE.search(text)
    return int(m.group(1)) if m else None


def _iter_blocks(
    text: str, keyword: str, start: int = 0, limit: int | None = None
) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` (end inclusive) of every ``(keyword …)`` block.

    A delimiter must follow the keyword, so ``(variant`` never matches
    ``(variants``. Nested same-keyword blocks are not descended into — each
    yielded block is skipped wholesale.
    """
    hard_limit = len(text) if limit is None else limit
    needle = "(" + keyword
    pos = start
    while True:
        idx = text.find(needle, pos, hard_limit)
        if idx == -1:
            return
        after = idx + len(needle)
        if after < len(text) and text[after] not in " \t\r\n()":
            pos = idx + 1
            continue
        end = _walk_balanced_parens(text, idx)
        if end is None:
            pos = idx + 1
            continue
        yield idx, end
        pos = end + 1


def _block_name(text: str, start: int, end: int) -> str | None:
    """The first ``(name "…")`` inside a block (KiCad writes it first)."""
    m = _NAME_RE.search(text, start, end + 1)
    return _unescape_sexp_string(m.group(1)) if m else None


def _leaf_bool(scope: str, token: str) -> bool | None:
    m = re.search(rf"\({token}\s+(yes|no)\s*\)", scope)
    if m is None:
        return None
    return m.group(1) == "yes"


def _yesno(value: bool) -> str:
    return "yes" if value else "no"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _line_indent(text: str, pos: int) -> str:
    """Leading whitespace of the line containing *pos* (up to *pos*)."""
    line_start = text.rfind("\n", 0, pos) + 1
    prefix = text[line_start:pos]
    return prefix if prefix.strip() == "" else ""


def _indent_unit(parent_indent: str, child_indent: str) -> str:
    """One indentation step, derived from a real parent/child line pair."""
    if child_indent.startswith(parent_indent) and len(child_indent) > len(parent_indent):
        return child_indent[len(parent_indent):]
    return "\t"  # the KiCad 10 pretty-printer's unit


def _remove_block_lines(text: str, start: int, end: int) -> str:
    """Remove a block together with its whole lines (no blank line left)."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        line_start = start  # block does not own the line start
    eol = text.find("\n", end)
    if eol == -1:
        return text[:line_start].rstrip("\n") + "\n"
    return text[:line_start] + text[eol + 1:]


def _parse_variant_members(text: str, start: int, end: int) -> dict[str, Any]:
    """Members of one ``(variant …)`` block, bool tokens + ``fields`` dict."""
    scope = text[start:end + 1]
    members: dict[str, Any] = {}
    for token in set(_SCH_BOOL_TOKENS) | set(_PCB_BOOL_TOKENS):
        value = _leaf_bool(scope, token)
        if value is not None:
            members[token] = value
    fields: dict[str, str] = {}
    for fs, fe in _iter_blocks(text, "field", start, end + 1):
        fname = _block_name(text, fs, fe)
        vm = _VALUE_RE.search(text, fs, fe + 1)
        if fname is not None and vm is not None:
            fields[fname] = _unescape_sexp_string(vm.group(1))
    if fields:
        members["fields"] = fields
    return members


def _render_variant_block(
    name: str, members: dict[str, Any], bool_order: tuple[str, ...],
    indent: str, unit: str,
) -> str:
    """Render a ``(variant …)`` block in KiCad 10's own pretty-print shape."""
    lines = [f"{indent}(variant", f"{indent}{unit}(name {_quote(name)})"]
    for token in bool_order:
        if token in members:
            lines.append(f"{indent}{unit}({token} {_yesno(members[token])})")
    for fname, fvalue in members.get("fields", {}).items():
        lines.append(f"{indent}{unit}(field")
        lines.append(f"{indent}{unit}{unit}(name {_quote(fname)})")
        lines.append(f"{indent}{unit}{unit}(value {_quote(fvalue)})")
        lines.append(f"{indent}{unit})")
    lines.append(f"{indent})")
    return "\n".join(lines)


def _find_named_variant_block(
    text: str, name: str, start: int, limit: int
) -> tuple[int, int] | None:
    for vs, ve in _iter_blocks(text, "variant", start, limit):
        if _block_name(text, vs, ve) == name:
            return vs, ve
    return None


# ── .kicad_pro (JSON registry) ───────────────────────────────────────────────

def pro_list_variants(text: str) -> list[dict[str, str]]:
    """The ``schematic.variants`` registry entries (empty list if none)."""
    data = json.loads(text)
    schematic = data.get("schematic")
    raw = schematic.get("variants") if isinstance(schematic, dict) else None
    out: list[dict[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            item = {"name": entry["name"]}
            if entry.get("description"):
                item["description"] = str(entry["description"])
            out.append(item)
    return out


def _pro_dump(data: dict[str, Any], original: str) -> str:
    out = json.dumps(data, indent=2)
    if original.endswith("\n"):
        out += "\n"
    return out


def pro_upsert_variant(text: str, name: str, description: str | None = None) -> str:
    """Add or update a registry entry. Unchanged registry → unchanged bytes."""
    data = json.loads(text)
    schematic = data.setdefault("schematic", {})
    variants = schematic.setdefault("variants", [])
    for entry in variants:
        if isinstance(entry, dict) and entry.get("name") == name:
            if description is None or entry.get("description") == description:
                return text  # already present as requested — byte-identical
            entry["description"] = description
            return _pro_dump(data, text)
    new_entry: dict[str, str] = {"name": name}
    if description:
        new_entry["description"] = description
    variants.append(new_entry)
    return _pro_dump(data, text)


def pro_remove_variant(text: str, name: str) -> str:
    """Remove a registry entry. Absent entry → unchanged bytes."""
    data = json.loads(text)
    schematic = data.get("schematic")
    if not isinstance(schematic, dict):
        return text
    variants = schematic.get("variants")
    if not isinstance(variants, list):
        return text
    kept = [v for v in variants if not (isinstance(v, dict) and v.get("name") == name)]
    if len(kept) == len(variants):
        return text
    schematic["variants"] = kept
    return _pro_dump(data, text)


# ── .kicad_sch (per-symbol instance blocks) ──────────────────────────────────

def _sch_symbol_blocks(text: str) -> Iterator[tuple[int, int]]:
    """Placed symbol instance blocks (the ``(lib_symbols …)`` section skipped)."""
    lib_start = text.find("(lib_symbols")
    lib_end = -1
    if lib_start != -1:
        end = _walk_balanced_parens(text, lib_start)
        lib_end = end if end is not None else -1
    for s, e in _iter_blocks(text, "symbol"):
        if lib_start != -1 and lib_start <= s <= lib_end:
            continue
        yield s, e


def _find_symbol_path_block(
    text: str, reference: str
) -> tuple[int, int, int, int] | None:
    """``(sym_start, sym_end, path_start, path_end)`` for the instance whose
    per-path ``(reference "…")`` matches — authoritative under multi-instance
    sheets, unlike the symbol's base Reference property."""
    ref_re = re.compile(rf'\(reference\s+"{re.escape(reference)}"\s*\)')
    for s, e in _sch_symbol_blocks(text):
        for is_, ie in _iter_blocks(text, "instances", s, e + 1):
            for ps, pe in _iter_blocks(text, "path", is_, ie + 1):
                if ref_re.search(text, ps, pe + 1):
                    return s, e, ps, pe
    return None


def _sch_base_scope(text: str, sym_start: int, sym_end: int) -> str:
    """Symbol text before its ``(instances`` section — where the base
    attribute tokens and properties live (variant blocks never do)."""
    inst = text.find("(instances", sym_start, sym_end)
    return text[sym_start:inst if inst != -1 else sym_end]


def _sch_base_attrs(scope: str) -> dict[str, bool]:
    def base(token: str, default: bool) -> bool:
        value = _leaf_bool(scope, token)
        return default if value is None else value

    return {
        "dnp": base("dnp", False),
        "in_bom": base("in_bom", True),
        "in_pos_files": base("in_pos_files", True),
        "exclude_from_sim": base("exclude_from_sim", False),
    }


def _base_fields(scope: str) -> dict[str, str]:
    return {
        _unescape_sexp_string(m.group(1)): _unescape_sexp_string(m.group(2))
        for m in _PROP_RE.finditer(scope)
    }


def _merge_members(
    existing: dict[str, Any],
    updates: dict[str, bool | None],
    base: dict[str, bool],
    base_fields: dict[str, str],
    new_fields: dict[str, str],
) -> dict[str, Any]:
    """Merge existing members with updates, then normalize against base
    (omit-when-equal-to-base). Bool keys are side-specific tokens."""
    members: dict[str, Any] = {}
    for token, update in updates.items():
        current = existing.get(token) if update is None else update
        if current is not None and current != base[token]:
            members[token] = current
    fields: dict[str, str] = dict(existing.get("fields", {}))
    fields.update(new_fields)
    fields = {k: v for k, v in fields.items() if base_fields.get(k) != v}
    if fields:
        members["fields"] = fields
    return members


def _splice_variant_block(
    text: str,
    scope_start: int,
    scope_end: int,
    anchor_pos: int,
    name: str,
    members: dict[str, Any],
    bool_order: tuple[str, ...],
    insert_before: int,
) -> str:
    """Replace the named variant block inside a scope, or insert a new one.

    *anchor_pos* is the start of an existing sibling line whose indentation
    the new block copies; *insert_before* is where a new block's line goes
    (start of the line holding the scope's closing paren, or an inner anchor).
    """
    indent = _line_indent(text, anchor_pos)
    parent_indent = _line_indent(text, scope_start)
    unit = _indent_unit(parent_indent, indent)
    block = _render_variant_block(name, members, bool_order, indent, unit)

    span = _find_named_variant_block(text, name, scope_start, scope_end + 1)
    if span is not None:
        vs, ve = span
        return text[:vs] + block[len(indent):] + text[ve + 1:]
    line_start = text.rfind("\n", 0, insert_before) + 1
    return text[:line_start] + block + "\n" + text[line_start:]


def sch_read_symbol(text: str, reference: str) -> dict[str, Any] | None:
    """Base attrs, base fields, and existing variant members for a symbol."""
    loc = _find_symbol_path_block(text, reference)
    if loc is None:
        return None
    sym_s, sym_e, ps, pe = loc
    scope = _sch_base_scope(text, sym_s, sym_e)
    variants = {}
    for vs, ve in _iter_blocks(text, "variant", ps, pe + 1):
        name = _block_name(text, vs, ve)
        if name is not None:
            variants[name] = _parse_variant_members(text, vs, ve)
    return {
        "base": _sch_base_attrs(scope),
        "base_fields": _base_fields(scope),
        "variants": variants,
    }


def sch_set_variant(
    text: str, reference: str, variant: str, overrides: VariantOverrides
) -> str:
    """Set (merge + normalize) one symbol's override block for one variant.

    An all-defaults result still writes a name-only block — membership marker.
    """
    loc = _find_symbol_path_block(text, reference)
    if loc is None:
        raise VariantAuthorError(
            f"reference {reference!r} not found in schematic"
        )
    sym_s, sym_e, ps, pe = loc
    scope = _sch_base_scope(text, sym_s, sym_e)
    base = _sch_base_attrs(scope)
    base_fields = _base_fields(scope)

    existing: dict[str, Any] = {}
    span = _find_named_variant_block(text, variant, ps, pe + 1)
    if span is not None:
        existing = _parse_variant_members(text, *span)

    members = _merge_members(
        existing,
        {
            "dnp": overrides.dnp,
            "in_bom": overrides.in_bom,
            "in_pos_files": overrides.in_pos_files,
            "exclude_from_sim": overrides.exclude_from_sim,
        },
        base,
        base_fields,
        overrides.fields,
    )

    # Anchor indentation on the (reference …) line inside the path block.
    ref_idx = text.find("(reference", ps, pe)
    anchor = ref_idx if ref_idx != -1 else ps
    return _splice_variant_block(
        text, ps, pe, anchor, variant, members, _SCH_BOOL_TOKENS, insert_before=pe,
    )


def sch_remove_variant(text: str, name: str) -> tuple[str, int]:
    """Remove the named variant's block from every symbol. Returns count."""
    spans: list[tuple[int, int]] = []
    for s, e in _sch_symbol_blocks(text):
        for is_, ie in _iter_blocks(text, "instances", s, e + 1):
            for ps, pe in _iter_blocks(text, "path", is_, ie + 1):
                span = _find_named_variant_block(text, name, ps, pe + 1)
                if span is not None:
                    spans.append(span)
    for vs, ve in sorted(spans, reverse=True):
        text = _remove_block_lines(text, vs, ve)
    return text, len(spans)


def sch_list_overrides(text: str) -> dict[str, dict[str, dict[str, Any]]]:
    """``{reference: {variant_name: members}}`` for every symbol instance."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    ref_re = re.compile(rf'\(reference\s+{_QSTR}\s*\)')
    for s, e in _sch_symbol_blocks(text):
        for is_, ie in _iter_blocks(text, "instances", s, e + 1):
            for ps, pe in _iter_blocks(text, "path", is_, ie + 1):
                ref_m = ref_re.search(text, ps, pe + 1)
                if ref_m is None:
                    continue
                reference = _unescape_sexp_string(ref_m.group(1))
                for vs, ve in _iter_blocks(text, "variant", ps, pe + 1):
                    name = _block_name(text, vs, ve)
                    if name is None:
                        continue
                    out.setdefault(reference, {})[name] = (
                        _parse_variant_members(text, vs, ve)
                    )
    return out


# ── .kicad_pcb (board registry + per-footprint blocks) ───────────────────────

def _pcb_board_variants_block(text: str) -> tuple[int, int] | None:
    for s, e in _iter_blocks(text, "variants"):
        return s, e
    return None


def pcb_list_board_variants(text: str) -> list[dict[str, str]]:
    """Entries of the board-level ``(variants …)`` registry."""
    span = _pcb_board_variants_block(text)
    if span is None:
        return []
    out: list[dict[str, str]] = []
    for vs, ve in _iter_blocks(text, "variant", span[0] + 1, span[1] + 1):
        name = _block_name(text, vs, ve)
        if name is None:
            continue
        entry = {"name": name}
        dm = re.search(rf"\(description\s+{_QSTR}", text[vs:ve + 1])
        if dm:
            entry["description"] = _unescape_sexp_string(dm.group(1))
        out.append(entry)
    return out


def _render_board_variant_entry(
    name: str, description: str | None, indent: str, unit: str
) -> str:
    lines = [f"{indent}(variant", f"{indent}{unit}(name {_quote(name)})"]
    if description:
        lines.append(f"{indent}{unit}(description {_quote(description)})")
    lines.append(f"{indent})")
    return "\n".join(lines)


def pcb_upsert_board_variant(
    text: str, name: str, description: str | None = None
) -> str:
    """Add/update an entry in the board registry, creating the registry after
    the ``(setup …)`` section if the board has none yet."""
    span = _pcb_board_variants_block(text)
    if span is None:
        # Create the whole (variants …) section.
        anchor = None
        for ss, se in _iter_blocks(text, "setup"):
            anchor = se
            break
        if anchor is None:
            fps = text.find("(footprint ")
            insert_at = text.rfind("\n", 0, fps) + 1 if fps != -1 else text.rfind(")")
            indent = "\t"
        else:
            insert_at = text.find("\n", anchor) + 1
            if insert_at == 0:
                insert_at = anchor + 1
            indent = _line_indent(text, text.rfind("(setup", 0, anchor + 1))
        unit = "\t" if "\t" in text else "  "
        entry = _render_board_variant_entry(name, description, indent + unit, unit)
        block = f"{indent}(variants\n{entry}\n{indent})\n"
        return text[:insert_at] + block + text[insert_at:]

    bs, be = span
    existing = _find_named_variant_block(text, name, bs + 1, be + 1)
    indent = _line_indent(text, bs)
    unit = "\t" if "\t" in text else "  "
    if existing is not None:
        vs, ve = existing
        for reg_entry in pcb_list_board_variants(text):
            if reg_entry["name"] == name:
                same_desc = reg_entry.get("description") == description
                if description is None or same_desc:
                    return text  # already as requested
        child_indent = _line_indent(text, vs)
        rendered = _render_board_variant_entry(name, description, child_indent, unit)
        return text[:vs] + rendered[len(child_indent):] + text[ve + 1:]
    # Append before the registry's closing paren.
    line_start = text.rfind("\n", 0, be) + 1
    entry_text = _render_board_variant_entry(name, description, indent + unit, unit)
    return text[:line_start] + entry_text + "\n" + text[line_start:]


def pcb_remove_board_variant(text: str, name: str) -> str:
    """Remove a registry entry (and the registry itself when it empties)."""
    span = _pcb_board_variants_block(text)
    if span is None:
        return text
    existing = _find_named_variant_block(text, name, span[0] + 1, span[1] + 1)
    if existing is None:
        return text
    text = _remove_block_lines(text, *existing)
    span = _pcb_board_variants_block(text)
    if span is not None and not pcb_list_board_variants(text):
        text = _remove_block_lines(text, *span)
    return text


def _pcb_footprint_scope(text: str, reference: str) -> tuple[int, int]:
    span = find_footprint_block_by_reference(text, reference)
    if span is None:
        raise VariantAuthorError(f"reference {reference!r} not found on board")
    return span


def _fp_scope_without_variants(text: str, fs: int, fe: int) -> str:
    """Footprint text with variant blocks cut out — safe for base-attr scans
    (a variant's own ``(dnp yes)`` must not read as the base attribute)."""
    parts: list[str] = []
    pos = fs
    for vs, ve in _iter_blocks(text, "variant", fs, fe + 1):
        parts.append(text[pos:vs])
        pos = ve + 1
    parts.append(text[pos:fe + 1])
    return "".join(parts)


def pcb_footprint_base_attrs(text: str, reference: str) -> dict[str, bool]:
    """Base dnp/exclusion state of a footprint (positive-logic keys)."""
    fs, fe = _pcb_footprint_scope(text, reference)
    scope = _fp_scope_without_variants(text, fs, fe)
    attr_m = re.search(r"\(attr\s+([^)]*)\)", scope)
    attr_tokens = set(attr_m.group(1).split()) if attr_m else set()

    def flag(token: str) -> bool:
        value = _leaf_bool(scope, token)
        return token in attr_tokens if value is None else value

    return {
        "dnp": flag("dnp"),
        "exclude_from_bom": flag("exclude_from_bom"),
        "exclude_from_pos_files": flag("exclude_from_pos_files"),
    }


def pcb_set_footprint_variant(
    text: str, reference: str, variant: str, overrides: VariantOverrides
) -> str:
    """Set one footprint's override block — the sch/pcb logic inversion
    (``in_bom=False`` → ``(exclude_from_bom yes)``) happens here and only
    here. ``exclude_from_sim`` has no board-side token and is ignored."""
    fs, fe = _pcb_footprint_scope(text, reference)
    base = pcb_footprint_base_attrs(text, reference)
    scope = _fp_scope_without_variants(text, fs, fe)
    base_fields = _base_fields(scope)

    existing: dict[str, Any] = {}
    span = _find_named_variant_block(text, variant, fs, fe + 1)
    if span is not None:
        existing = _parse_variant_members(text, *span)

    members = _merge_members(
        existing,
        {
            "dnp": overrides.dnp,
            "exclude_from_bom": None if overrides.in_bom is None else not overrides.in_bom,
            "exclude_from_pos_files": (
                None if overrides.in_pos_files is None else not overrides.in_pos_files
            ),
        },
        base,
        base_fields,
        overrides.fields,
    )

    # Insertion point mirrors pcbnew's writer: after pads, before
    # (embedded_fonts …) / (model …) / the closing paren.
    insert_before = fe
    for marker in ("(embedded_fonts", "(model "):
        idx = text.find(marker, fs, fe)
        if idx != -1:
            insert_before = idx
            break
    anchor = insert_before if insert_before != fe else fs
    # Indentation anchor: a real child line of the footprint.
    child_idx = text.find("(attr", fs, fe)
    if child_idx == -1:
        child_idx = text.find("(property", fs, fe)
    anchor = child_idx if child_idx != -1 else anchor
    return _splice_variant_block(
        text, fs, fe, anchor, variant, members, _PCB_BOOL_TOKENS,
        insert_before=insert_before,
    )


def pcb_remove_footprint_variants(text: str, name: str) -> tuple[str, int]:
    """Remove the named variant's block from every footprint. Returns count."""
    spans: list[tuple[int, int]] = []
    board_span = _pcb_board_variants_block(text)
    for vs, ve in _iter_blocks(text, "variant"):
        if board_span is not None and board_span[0] <= vs <= board_span[1]:
            continue
        if _block_name(text, vs, ve) == name:
            spans.append((vs, ve))
    for vs, ve in sorted(spans, reverse=True):
        text = _remove_block_lines(text, vs, ve)
    return text, len(spans)


def pcb_list_footprint_overrides(text: str) -> dict[str, dict[str, dict[str, Any]]]:
    """``{reference: {variant_name: members}}`` for every footprint."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    ref_re = re.compile(rf'\(property\s+"Reference"\s+{_QSTR}')
    for fps, fpe in _iter_blocks(text, "footprint"):
        ref_m = ref_re.search(text, fps, fpe + 1)
        if ref_m is None:
            continue
        reference = _unescape_sexp_string(ref_m.group(1))
        for vs, ve in _iter_blocks(text, "variant", fps, fpe + 1):
            name = _block_name(text, vs, ve)
            if name is None:
                continue
            out.setdefault(reference, {})[name] = _parse_variant_members(text, vs, ve)
    return out


# ── write session (REQ-S4B-006/009) ──────────────────────────────────────────

_PROJECT_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")


@dataclass
class ProjectFiles:
    """The resolved project trio (sch/pcb may be absent)."""

    pro: Path
    sch: Path | None
    pcb: Path | None

    @property
    def directory(self) -> Path:
        return self.pro.parent


def resolve_project(any_path: Path) -> ProjectFiles:
    """Resolve the project trio from any of its three files (by stem)."""
    if any_path.suffix not in _PROJECT_SUFFIXES:
        raise VariantAuthorError(
            f"expected a {'/'.join(_PROJECT_SUFFIXES)} path, got {any_path.name!r}"
        )
    base = any_path.with_suffix("")
    pro = base.with_suffix(".kicad_pro")
    if not pro.exists():
        raise VariantAuthorError(
            f"not a KiCad project: {pro.name} not found next to {any_path.name}"
        )
    sch = base.with_suffix(".kicad_sch")
    pcb = base.with_suffix(".kicad_pcb")
    return ProjectFiles(
        pro=pro,
        sch=sch if sch.exists() else None,
        pcb=pcb if pcb.exists() else None,
    )


def check_format_versions(files: ProjectFiles) -> dict[str, Any] | None:
    """REQ-S4B-008: refuse to author variant tokens into pre-v10 files.

    Returns the structured ``format_too_old`` payload, or None when OK.
    """
    checks: list[tuple[Path, int]] = []
    if files.sch is not None:
        checks.append((files.sch, SCH_MIN_FORMAT))
    if files.pcb is not None:
        checks.append((files.pcb, PCB_MIN_FORMAT))
    for path, minimum in checks:
        stamp = file_format_version(path.read_text(encoding="utf-8"))
        if stamp is None or stamp < minimum:
            return {
                "error": "format_too_old",
                "file": str(path),
                "found": stamp,
                "required": minimum,
                "hint": (
                    "This file predates KiCad 10's variant format. Run the "
                    "upgrade_project_format tool on this project (it backs up every "
                    "file, then upgrades the schematic and board in place), then "
                    "retry. Note: KiCad 9 cannot open upgraded files — the v9 "
                    "backups in .kicad_mcp_backups/ are your only rollback."
                ),
            }
    return None


class VariantWriteError(Exception):
    """A write session that failed validation or hit a conflict."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


class VariantWriteSession:
    """Write-validate-commit over the project trio (REQ-S4B-006).

    Usage: construct (snapshots mtimes + loads texts), ``stage()`` new
    content per file, then ``commit(cli_path)`` — which validates staged
    content with kicad-cli oracles in a scratch copy of the project, then
    atomically replaces the originals (all-or-nothing). A failed validation
    or a concurrent modification leaves every original byte-identical.
    """

    def __init__(self, files: ProjectFiles) -> None:
        self.files = files
        self._staged: dict[Path, str] = {}
        self.texts: dict[Path, str] = {}
        self._mtimes: dict[Path, int] = {}
        for path in (files.pro, files.sch, files.pcb):
            if path is not None:
                self.texts[path] = path.read_text(encoding="utf-8")
                self._mtimes[path] = path.stat().st_mtime_ns

    def stage(self, path: Path, new_text: str) -> bool:
        """Stage new content; returns False (and stages nothing) if identical."""
        if new_text == self.texts[path]:
            return False
        self._staged[path] = new_text
        return True

    @property
    def staged_paths(self) -> list[Path]:
        return list(self._staged)

    def _erc_total(self, ops: Any, sch_path: Path) -> int | None:
        result = ops.run_erc(sch_path)
        report = Path(result.get("report_file", ""))
        if not report.exists():
            return None
        counts = int(result.get("error_count", 0)) + int(result.get("warning_count", 0))
        return counts

    def commit(self, cli_path: Path) -> dict[str, Any]:
        """Validate staged content and atomically replace the originals."""
        if not self._staged:
            return {"changed": False, "files": []}

        from kicad_mcp.backends.cli_backend import CLIDRCOps  # local: avoid cycle

        ops = CLIDRCOps(cli_path)
        project_dir = self.files.directory
        scratch = project_dir / f".variant_write_tmp_{os.getpid()}"
        validated: dict[str, Any] = {}
        try:
            for sub in ("orig", "new"):
                (scratch / sub).mkdir(parents=True, exist_ok=True)
            # Copy the full project (pro + every sch sheet + pcb) so the
            # oracles see real stems, project settings, and sheet hierarchy.
            members = [self.files.pro, self.files.pcb]
            members += sorted(project_dir.glob("*.kicad_sch"))
            for path in members:
                if path is None or not path.exists():
                    continue
                for sub in ("orig", "new"):
                    shutil.copy2(path, scratch / sub / path.name)
            for path, new_text in self._staged.items():
                (scratch / "new" / path.name).write_text(new_text, encoding="utf-8")

            sch_staged = self.files.sch is not None and self.files.sch in self._staged
            pcb_staged = self.files.pcb is not None and self.files.pcb in self._staged

            if sch_staged and self.files.sch is not None:
                baseline = self._erc_total(ops, scratch / "orig" / self.files.sch.name)
                authored = self._erc_total(ops, scratch / "new" / self.files.sch.name)
                if authored is None:
                    raise VariantWriteError("ERC validation failed", {
                        "error": "validation_failed",
                        "stage": "erc",
                        "message": "kicad-cli sch erc produced no report for the "
                                   "authored schematic — write aborted, files untouched.",
                    })
                if baseline is not None and authored != baseline:
                    raise VariantWriteError("ERC count changed", {
                        "error": "validation_failed",
                        "stage": "erc",
                        "message": (
                            f"authoring changed the ERC violation count "
                            f"({baseline} -> {authored}) — write aborted, files untouched."
                        ),
                    })
                validated["erc"] = {"baseline": baseline, "authored": authored}

            if pcb_staged and self.files.pcb is not None:
                drc = ops.run_drc(scratch / "new" / self.files.pcb.name)
                report = Path(drc.get("report_file", ""))
                if not report.exists():
                    raise VariantWriteError("DRC validation failed", {
                        "error": "validation_failed",
                        "stage": "drc",
                        "message": "kicad-cli pcb drc could not parse the authored "
                                   "board — write aborted, files untouched.",
                    })
                validated["drc"] = "parsed"

            # Stale guard: nothing may have changed the originals meanwhile.
            for path in self._staged:
                if path.stat().st_mtime_ns != self._mtimes[path]:
                    raise VariantWriteError("concurrent modification", {
                        "error": "stale_file",
                        "file": str(path),
                        "message": f"{path.name} changed on disk during the write "
                                   "session — aborted, no files replaced. Re-read "
                                   "and retry.",
                    })

            # RW-S4 §9: refuse while KiCad holds any staged file open — the
            # replace below bypasses the per-operation write guards.
            from kicad_mcp.backends.file_backend import (  # local: avoid cycle
                BoardOpenInKiCadError,
                _refuse_if_open_in_kicad,
            )
            for path in self._staged:
                try:
                    _refuse_if_open_in_kicad(path)
                except BoardOpenInKiCadError as exc:
                    raise VariantWriteError("file open in KiCad", {
                        "error": "schematic_open_in_kicad",
                        "lock_file": str(exc.lock_file),
                        "lock_holder": exc.lock_holder,
                        "message": str(exc),
                    }) from exc

            for path in self._staged:
                os.replace(scratch / "new" / path.name, path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        return {
            "changed": True,
            "files": [p.name for p in self._staged],
            "validated_by": validated,
        }
