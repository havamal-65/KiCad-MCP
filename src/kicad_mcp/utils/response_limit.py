"""Utilities to keep MCP tool responses within token limits.

AI assistants (Claude, GPT-4, etc.) have hard limits on the number of tokens
a tool response may contain. A large KiCad schematic can have hundreds of
symbols and thousands of wires; serialising everything at once easily exceeds
those limits. This module provides helpers that cap list fields before the
result is JSON-serialised so the response stays within a safe budget.
"""
from __future__ import annotations

import json
from typing import Any

# ~80 000 chars ≈ 20 000 tokens — comfortably below the common 32 000-token ceiling.
MAX_RESPONSE_CHARS = 80_000
_DEFAULT_MAX_ITEMS = 100


def cap_lists(data: dict[str, Any], max_items: int = _DEFAULT_MAX_ITEMS) -> dict[str, Any]:
    """Recursively cap all list fields in *data* to *max_items* entries.

    When a list is shortened the function adds two sibling keys so callers
    know the result is partial::

        "symbols_total": 342,
        "symbols_truncated": true,

    Nested dicts are processed recursively; all other values are left as-is.
    """
    result: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, list):
            total = len(val)
            if total > max_items:
                result[key] = val[:max_items]
                result[f"{key}_total"] = total
                result[f"{key}_truncated"] = True
            else:
                result[key] = val
        elif isinstance(val, dict):
            result[key] = cap_lists(val, max_items)
        else:
            result[key] = val
    return result


def limit_response(data: dict[str, Any]) -> dict[str, Any]:
    """Return *data* capped so its JSON representation stays within MAX_RESPONSE_CHARS.

    Tries progressively smaller list limits (100 → 50 → 25 → 10) until the
    serialised size fits.  The tightest cap is returned regardless of whether
    the final size is within budget (the caller should still serialise and
    return the result — a slightly oversized but truncated response is better
    than an error).
    """
    for max_items in (_DEFAULT_MAX_ITEMS, 50, 25, 10):
        capped = cap_lists(data, max_items)
        if len(json.dumps(capped)) <= MAX_RESPONSE_CHARS:
            return capped
    return capped
