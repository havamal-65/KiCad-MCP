"""Persisted launcher UI settings — pure, import-safe (no GUI).

Small JSON blob stored next to recents (`%APPDATA%/kicad-mcp-launcher/`):
layout variant, window position, per-variant width. Reads are tolerant
(corrupt/missing → defaults); writes are atomic (same pattern as recents).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from launcher.config import LauncherConfig

DEFAULTS: dict[str, Any] = {
    "variant": "console",           # 'console' | 'bento'
    "window_x": None,               # last position (None -> centered by the OS)
    "window_y": None,
    "width_console": 504,
    "width_bento": 830,
}

_VALID_VARIANTS = ("console", "bento")


def _path(cfg: LauncherConfig) -> Path:
    return cfg.recents_path.parent / "settings.json"


def load_settings(cfg: LauncherConfig) -> dict[str, Any]:
    """Load settings merged over defaults. Corrupt/missing file → defaults."""
    out = dict(DEFAULTS)
    try:
        raw = _path(cfg).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for key in DEFAULTS:
        if key in data:
            out[key] = data[key]
    if out["variant"] not in _VALID_VARIANTS:
        out["variant"] = DEFAULTS["variant"]
    for key in ("window_x", "window_y", "width_console", "width_bento"):
        if out[key] is not None and not isinstance(out[key], (int, float)):
            out[key] = DEFAULTS[key]
    return out


def save_settings(cfg: LauncherConfig, **updates: Any) -> dict[str, Any]:
    """Merge `updates` into the stored settings atomically; returns the result."""
    current = load_settings(cfg)
    for key, value in updates.items():
        if key in DEFAULTS:
            current[key] = value
    if current["variant"] not in _VALID_VARIANTS:
        current["variant"] = DEFAULTS["variant"]
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return current


def width_for(settings: dict[str, Any], variant: str) -> int:
    key = "width_bento" if variant == "bento" else "width_console"
    try:
        return max(420, int(settings.get(key) or DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])
