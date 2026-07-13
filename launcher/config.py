"""Launcher configuration — pure, import-safe (no GUI, no tkinter).

`LauncherConfig` resolves every path/port the launcher needs; `load_config()`
builds it from the environment with sensible repo-relative defaults. All fields
are overridable via environment variables so the launcher works from a checkout
without a config file (REQ-* config).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# launcher/config.py -> parents[1] == repo root
REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765  # matches the committed .mcp.dev.json URL


@dataclass(frozen=True)
class LauncherConfig:
    venv_python: Path
    venv_pythonw: Path
    mcp_host: str
    mcp_port: int
    mcp_config_path: Path
    projects_roots: list[Path]
    recents_path: Path


def _venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _venv_pythonw(root: Path) -> Path:
    if sys.platform == "win32":
        pyw = root / ".venv" / "Scripts" / "pythonw.exe"
        return pyw if pyw.exists() else _venv_python(root)
    # No pythonw equivalent off Windows.
    return _venv_python(root)


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "kicad-mcp-launcher"
    return Path.home() / ".config" / "kicad-mcp-launcher"


def _default_projects_roots(root: Path) -> list[Path]:
    candidates = [root / "examples", Path.home() / "Documents" / "KiCad"]
    return [p for p in candidates if p.exists()]


def _env_projects_roots() -> list[Path]:
    raw = os.environ.get("KICAD_MCP_PROJECTS_ROOT")
    if not raw:
        return []
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def load_config(root: Path | None = None) -> LauncherConfig:
    """Build a `LauncherConfig` from the environment.

    `root` defaults to the repo root inferred from this file's location; it is
    injectable so tests can point at a scratch tree.
    """
    root = root or REPO_ROOT

    host = os.environ.get("KICAD_MCP_HTTP_HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("KICAD_MCP_HTTP_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    mcp_config_path = Path(
        os.environ.get("KICAD_MCP_LAUNCHER_MCP_CONFIG", str(root / ".mcp.dev.json"))
    )

    roots = _env_projects_roots() + _default_projects_roots(root)
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    projects_roots: list[Path] = []
    for p in roots:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            projects_roots.append(p)

    recents_path = _appdata_dir() / "recents.json"

    return LauncherConfig(
        venv_python=_venv_python(root),
        venv_pythonw=_venv_pythonw(root),
        mcp_host=host,
        mcp_port=port,
        mcp_config_path=mcp_config_path,
        projects_roots=projects_roots,
        recents_path=recents_path,
    )
