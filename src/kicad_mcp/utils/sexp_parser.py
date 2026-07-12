"""S-expression parser for KiCad files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kicad_mcp.models.errors import InvalidFileFormatError


def parse_sexp_file(path: Path) -> list[Any]:
    """Parse a KiCad S-expression file into a nested list structure.

    Uses sexpdata if available, otherwise falls back to a simple parser.

    Args:
        path: Path to the .kicad_pcb, .kicad_sch, .kicad_sym, or .kicad_mod file.

    Returns:
        Nested list representing the S-expression tree. Each node is either:
        - A string (atom)
        - A number (int or float)
        - A list (compound expression)
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InvalidFileFormatError(f"Cannot read file: {e}")
    return parse_sexp_content(content, source=str(path))


def parse_sexp_content(content: str, source: str = "<string>") -> list[Any]:
    """Parse already-loaded S-expression text into a nested list structure.

    Lets callers avoid a second disk read when they already hold the file
    contents (bulk operations that mutate the same string they parse).

    Args:
        content: The raw S-expression text.
        source: Optional descriptor used in error messages (e.g. a path).
    """
    if not content.strip().startswith("("):
        raise InvalidFileFormatError(f"Not a valid S-expression file: {source}")

    try:
        import sexpdata
        parsed = sexpdata.loads(content)
        result: list[Any] = _normalize_sexpdata(parsed)
        return result
    except ImportError:
        return _simple_parse(content)


def _normalize_sexpdata(data: Any) -> Any:
    """Convert sexpdata types to plain Python types."""
    import sexpdata

    if isinstance(data, sexpdata.Symbol):
        return str(data)
    elif isinstance(data, list):
        return [_normalize_sexpdata(item) for item in data]
    elif isinstance(data, str):
        return data
    elif isinstance(data, (int, float)):
        return data
    else:
        return str(data)


def _simple_parse(content: str) -> list[Any]:
    """Simple S-expression parser fallback when sexpdata is not available.

    Handles the subset of S-expressions used by KiCad files.
    """
    tokens = _tokenize(content)
    result, _ = _parse_tokens(tokens, 0)
    # Return the children of the top-level expression
    if isinstance(result, list) and len(result) > 0:
        return result
    return []


def _tokenize(content: str) -> list[str]:
    """Tokenize an S-expression string."""
    tokens: list[str] = []
    i = 0
    length = len(content)

    while i < length:
        ch = content[i]

        if ch in (" ", "\t", "\n", "\r"):
            i += 1
            continue

        if ch == "(":
            tokens.append("(")
            i += 1
            continue

        if ch == ")":
            tokens.append(")")
            i += 1
            continue

        if ch == '"':
            # Quoted string
            j = i + 1
            while j < length:
                if content[j] == "\\" and j + 1 < length:
                    j += 2
                    continue
                if content[j] == '"':
                    break
                j += 1
            tokens.append(content[i + 1 : j])
            i = j + 1
            continue

        # Unquoted atom
        j = i
        while j < length and content[j] not in (" ", "\t", "\n", "\r", "(", ")"):
            j += 1
        tokens.append(content[i:j])
        i = j

    return tokens


def _parse_tokens(tokens: list[str], pos: int) -> tuple[Any, int]:
    """Parse tokens starting at position, returning (result, new_position)."""
    if pos >= len(tokens):
        return [], pos

    if tokens[pos] == "(":
        # Parse list
        result = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ")":
            item, pos = _parse_tokens(tokens, pos)
            result.append(item)
        if pos < len(tokens):
            pos += 1  # skip closing )
        return result, pos

    # Atom
    token = tokens[pos]
    # Try to convert to number
    try:
        return int(token), pos + 1
    except ValueError:
        pass
    try:
        return float(token), pos + 1
    except ValueError:
        pass
    return token, pos + 1


def _walk_balanced_parens(content: str, start: int) -> int | None:
    """Walk forward from an opening paren to find the matching close paren.

    Handles quoted strings (including escaped quotes) correctly.

    Args:
        content: Full text.
        start: Index of the opening ``(`` character.

    Returns:
        Index of the matching ``)`` (inclusive), or ``None`` if unbalanced.
    """
    depth = 0
    i = start
    while i < len(content):
        ch = content[i]
        if ch == '"':
            # Skip quoted strings (handle escaped quotes)
            i += 1
            while i < len(content):
                if content[i] == '\\' and i + 1 < len(content):
                    i += 2
                    continue
                if content[i] == '"':
                    break
                i += 1
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def find_symbol_block_by_reference(content: str, reference: str) -> tuple[int, int] | None:
    """Locate a schematic symbol instance block by its Reference property.

    Schematic symbol instances look like::

        (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
          ...
          (property "Reference" "R1" (at 100 48 0) ...)
          ...
        )

    This function scans for every ``(symbol `` occurrence, extracts the full
    balanced block, and checks whether the block contains a
    ``(property "Reference" "<reference>" ...)`` child.

    The ``(lib_symbols ...)`` section is skipped since it contains library
    definitions, not placed instances.

    Args:
        content: Full schematic file text.
        reference: The reference designator to find (e.g. ``"R1"``).

    Returns:
        ``(start_index, end_index)`` of the block in the text (end is
        inclusive of the closing ``)``) , or ``None`` if not found.
    """
    escaped_ref = re.escape(reference)
    ref_pattern = re.compile(
        rf'\(property\s+"Reference"\s+"{escaped_ref}"'
    )

    # Find the lib_symbols section so we can skip it
    lib_symbols_start = content.find("(lib_symbols")
    lib_symbols_end = -1
    if lib_symbols_start != -1:
        end = _walk_balanced_parens(content, lib_symbols_start)
        if end is not None:
            lib_symbols_end = end

    # Scan for all (symbol occurrences
    search_start = 0
    while True:
        idx = content.find("(symbol ", search_start)
        if idx == -1:
            break

        # Skip if inside lib_symbols section
        if lib_symbols_start != -1 and lib_symbols_start <= idx <= lib_symbols_end:
            search_start = lib_symbols_end + 1
            continue

        # Walk balanced parens to find the full block
        end = _walk_balanced_parens(content, idx)
        if end is None:
            search_start = idx + 1
            continue

        block_text = content[idx:end + 1]

        # Check if this block has the matching Reference property
        if ref_pattern.search(block_text):
            return (idx, end)

        search_start = end + 1

    return None


def remove_sexp_block(content: str, start: int, end: int) -> str:
    """Remove an S-expression block from file content and clean up whitespace.

    Removes the text from ``start`` to ``end`` (inclusive) and collapses any
    resulting blank lines down to a single newline.

    Args:
        content: Full file text.
        start: Start index of the block to remove.
        end: End index of the block (inclusive).

    Returns:
        The modified file content with the block removed.
    """
    before = content[:start]
    after = content[end + 1:]

    # Remove the blank/whitespace-only line(s) left behind by the removal.
    # Trim trailing whitespace from the part before the block.
    before = before.rstrip(" \t\n")
    # Trim leading whitespace/newlines from the part after the block.
    after = after.lstrip(" \t\n")

    # Rejoin with exactly two newlines (one blank line separator) if both
    # sides have content, otherwise just a newline.
    if before and after:
        return before + "\n\n" + after
    elif before:
        return before + "\n"
    else:
        return after


def find_footprint_block_by_reference(content: str, reference: str) -> tuple[int, int] | None:
    """Locate a PCB footprint block by its Reference property.

    PCB footprint instances look like::

        (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 100 50)
          ...
          (property "Reference" "R1" ...)
          ...
        )

    This function scans for every ``(footprint `` occurrence, extracts the
    full balanced block, and checks whether the block contains a
    ``(property "Reference" "<reference>" ...)`` child.

    Also handles the older ``(fp_text reference "R1" ...)`` format.

    Args:
        content: Full PCB file text.
        reference: The reference designator to find (e.g. ``"R1"``).

    Returns:
        ``(start_index, end_index)`` of the block in the text (end is
        inclusive of the closing ``)``) , or ``None`` if not found.
    """
    escaped_ref = re.escape(reference)
    ref_pattern = re.compile(
        rf'\(property\s+"Reference"\s+"{escaped_ref}"'
    )
    fp_text_pattern = re.compile(
        rf'\(fp_text\s+reference\s+"{escaped_ref}"'
    )

    search_start = 0
    while True:
        idx = content.find("(footprint ", search_start)
        if idx == -1:
            break

        end = _walk_balanced_parens(content, idx)
        if end is None:
            search_start = idx + 1
            continue

        block_text = content[idx:end + 1]

        if ref_pattern.search(block_text) or fp_text_pattern.search(block_text):
            return (idx, end)

        search_start = end + 1

    return None


def _iter_wire_segments(
    content: str,
) -> "list[tuple[int, int, tuple[float, float, float, float]]]":
    """Enumerate wire blocks with their two endpoints.

    Handles both the compact one-line form this tool writes
    (``(wire (pts (xy ...) (xy ...)) ...)``) and KiCad's native multi-line
    form (``(wire\\n  (pts\\n    (xy ...) (xy ...)\\n  ) ...)``).

    Returns:
        List of ``(start_index, end_index, (x1, y1, x2, y2))`` tuples.
    """
    segments = []
    search_start = 0
    while True:
        idx = content.find("(wire", search_start)
        if idx == -1:
            break

        # Require a delimiter after the keyword so e.g. "(wires" never matches.
        after = idx + len("(wire")
        if after >= len(content) or content[after] not in " \t\r\n(":
            search_start = idx + 1
            continue

        end = _walk_balanced_parens(content, idx)
        if end is None:
            search_start = idx + 1
            continue

        block = content[idx:end + 1]
        xys = re.findall(r'\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\)', block)
        if len(xys) >= 2:
            segments.append((
                idx, end,
                (float(xys[0][0]), float(xys[0][1]),
                 float(xys[1][0]), float(xys[1][1])),
            ))

        search_start = end + 1

    return segments


def find_wire_block_by_endpoints(
    content: str,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    tolerance: float = 0.01,
) -> tuple[int, int] | None:
    """Locate a wire block by its start/end coordinates.

    Endpoints are compared numerically within *tolerance* (so ``63.5``
    matches ``63.50``), in both orders — a wire stored as end→start still
    matches a start→end query.

    Args:
        content: Full schematic file text.
        start_x: Expected start X coordinate.
        start_y: Expected start Y coordinate.
        end_x: Expected end X coordinate.
        end_y: Expected end Y coordinate.
        tolerance: Maximum allowed difference per coordinate (mm).

    Returns:
        ``(start_index, end_index)`` inclusive, or ``None`` if not found.
    """
    def _close(ax: float, ay: float, bx: float, by: float) -> bool:
        return abs(ax - bx) <= tolerance and abs(ay - by) <= tolerance

    for idx, end, (wx1, wy1, wx2, wy2) in _iter_wire_segments(content):
        forward = _close(wx1, wy1, start_x, start_y) and _close(wx2, wy2, end_x, end_y)
        reverse = _close(wx2, wy2, start_x, start_y) and _close(wx1, wy1, end_x, end_y)
        if forward or reverse:
            return (idx, end)

    return None


def find_nearest_wires(
    content: str,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    count: int = 3,
) -> list[dict[str, Any]]:
    """Rank wires by endpoint distance to the requested segment.

    Used to build actionable no-match diagnostics for ``remove_wire``.
    Distance is the smaller (over both endpoint pairings) sum of euclidean
    distances between requested and actual endpoints.

    Returns:
        Up to *count* dicts ``{"start": {x, y}, "end": {x, y}, "distance": mm}``
        sorted nearest-first.
    """
    import math

    candidates: list[dict[str, Any]] = []
    for _idx, _end, (wx1, wy1, wx2, wy2) in _iter_wire_segments(content):
        forward = (math.hypot(wx1 - start_x, wy1 - start_y)
                   + math.hypot(wx2 - end_x, wy2 - end_y))
        reverse = (math.hypot(wx2 - start_x, wy2 - start_y)
                   + math.hypot(wx1 - end_x, wy1 - end_y))
        candidates.append({
            "start": {"x": wx1, "y": wy1},
            "end": {"x": wx2, "y": wy2},
            "distance": round(min(forward, reverse), 4),
        })

    candidates.sort(key=lambda c: c["distance"])
    return candidates[:count]


def find_no_connect_block_by_position(
    content: str,
    x: float, y: float,
    tolerance: float = 0.01,
) -> tuple[int, int] | None:
    """Locate a no_connect block by its position.

    Scans for ``(no_connect (at x y) ...)`` blocks and checks whether
    the position matches within *tolerance*.

    Args:
        content: Full schematic file text.
        x: Expected X coordinate.
        y: Expected Y coordinate.
        tolerance: Maximum allowed difference per coordinate (mm).

    Returns:
        ``(start_index, end_index)`` inclusive, or ``None`` if not found.
    """
    search_start = 0
    while True:
        idx = content.find("(no_connect ", search_start)
        if idx == -1:
            break

        end = _walk_balanced_parens(content, idx)
        if end is None:
            search_start = idx + 1
            continue

        block = content[idx:end + 1]

        at_match = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)\)', block)
        if at_match:
            nx = float(at_match.group(1))
            ny = float(at_match.group(2))

            if abs(nx - x) <= tolerance and abs(ny - y) <= tolerance:
                return (idx, end)

        search_start = end + 1

    return None


_LABEL_KINDS = ("label", "global_label", "hierarchical_label")


def _unescape_sexp_string(raw: str) -> str:
    """Undo KiCad s-expression string escaping (\\" and \\\\)."""
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def _iter_label_blocks(
    content: str,
    kinds: tuple[str, ...] = _LABEL_KINDS,
) -> list[dict[str, Any]]:
    """Enumerate label blocks of the given kinds.

    Returns:
        List of ``{"start", "end", "kind", "text", "x", "y"}`` dicts where
        ``start``/``end`` are inclusive block indices into *content*.
    """
    blocks = []
    for kind in kinds:
        token = f"({kind}"
        search_start = 0
        while True:
            idx = content.find(token, search_start)
            if idx == -1:
                break

            after = idx + len(token)
            if after >= len(content) or content[after] not in " \t\r\n":
                search_start = idx + 1
                continue

            end = _walk_balanced_parens(content, idx)
            if end is None:
                search_start = idx + 1
                continue

            block = content[idx:end + 1]
            text_match = re.match(
                rf'\({kind}\s+"((?:[^"\\]|\\.)*)"', block,
            )
            at_match = re.search(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)', block)
            if text_match and at_match:
                blocks.append({
                    "start": idx,
                    "end": end,
                    "kind": kind,
                    "text": _unescape_sexp_string(text_match.group(1)),
                    "x": float(at_match.group(1)),
                    "y": float(at_match.group(2)),
                })

            search_start = end + 1

    return blocks


def find_label_block_by_position(
    content: str,
    x: float, y: float,
    text: str | None = None,
    tolerance: float = 0.01,
    kinds: tuple[str, ...] = _LABEL_KINDS,
) -> tuple[int, int] | None:
    """Locate a label block by its position, optionally filtered by text.

    Scans ``(label "..." (at x y angle) ...)`` blocks (plus global and
    hierarchical variants) and matches the position numerically within
    *tolerance*. When *text* is given, the label text must also match
    exactly (after unescaping).

    Returns:
        ``(start_index, end_index)`` inclusive, or ``None`` if not found.
    """
    for blk in _iter_label_blocks(content, kinds):
        if abs(blk["x"] - x) > tolerance or abs(blk["y"] - y) > tolerance:
            continue
        if text is not None and blk["text"] != text:
            continue
        return (blk["start"], blk["end"])

    return None


def find_nearest_labels(
    content: str,
    x: float, y: float,
    count: int = 3,
    kinds: tuple[str, ...] = _LABEL_KINDS,
) -> list[dict[str, Any]]:
    """Rank labels by distance to the requested position.

    Used to build actionable no-match diagnostics for the label edit ops.

    Returns:
        Up to *count* dicts ``{"text", "label_type", "position": {x, y},
        "distance": mm}`` sorted nearest-first.
    """
    import math

    candidates = [
        {
            "text": blk["text"],
            "label_type": blk["kind"],
            "position": {"x": blk["x"], "y": blk["y"]},
            "distance": round(math.hypot(blk["x"] - x, blk["y"] - y), 4),
        }
        for blk in _iter_label_blocks(content, kinds)
    ]
    candidates.sort(key=lambda c: c["distance"])
    return candidates[:count]


def extract_sexp_block(content: str, tag: str, name: str) -> str | None:
    """Extract a complete S-expression block from raw text by tag and name.

    Finds a block like ``(symbol "Name" ...)`` by matching balanced parentheses.
    This operates on raw text to avoid formatting drift from parse/serialize round-trips.

    Args:
        content: Full file text to search in.
        tag: The S-expression tag to match (e.g. ``"symbol"``).
        name: The quoted name value following the tag (e.g. ``"SCD41"``).

    Returns:
        The complete block text including outer parentheses, or ``None`` if not found.
    """
    # Build a pattern that matches (tag "name" with flexible whitespace
    escaped_name = re.escape(name)
    pattern = re.compile(
        rf'\({tag}\s+"{escaped_name}"'
    )

    match = pattern.search(content)
    if match is None:
        return None

    start = match.start()
    end = _walk_balanced_parens(content, start)
    if end is None:
        return None
    return content[start:end + 1]
