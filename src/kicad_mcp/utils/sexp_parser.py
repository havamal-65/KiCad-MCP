"""S-expression parser for KiCad files."""

from __future__ import annotations

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

    if not content.strip().startswith("("):
        raise InvalidFileFormatError(f"Not a valid S-expression file: {path}")

    try:
        import sexpdata
        parsed = sexpdata.loads(content)
        return _normalize_sexpdata(parsed)
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
