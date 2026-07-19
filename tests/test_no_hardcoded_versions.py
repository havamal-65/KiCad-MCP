"""AR1 enforcement: no KiCad version list lives outside the resolver.

K10-S1 centralized version knowledge in ``kicad_mcp/utils/kicad_version.py``.
This guard fails if a ``["9.0", "8.0", ...]``-style version list reappears
anywhere in ``src/`` or ``launcher/`` — the rule has to outlive the sprint that
established it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [REPO_ROOT / "src", REPO_ROOT / "launcher"]

# The sole legitimate owner of a version list.
ALLOWED = {REPO_ROOT / "src" / "kicad_mcp" / "utils" / "kicad_version.py"}

# Two adjacent quoted major.minor strings — the shape of a hardcoded list like
# ["9.0", "8.0", "7.0"] or ("9.0", "8.0"). A lone "9.0" (e.g. a default arg or a
# docstring example) is allowed; a *list* of them is what AR1 forbids.
VERSION_LIST_RE = re.compile(r'["\']\d+\.\d+["\']\s*,\s*["\']\d+\.\d+["\']')


def _iter_py_files():
    for base in SCAN_DIRS:
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_no_hardcoded_version_lists_outside_resolver():
    offenders: list[str] = []
    for path in _iter_py_files():
        if path in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if VERSION_LIST_RE.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Hardcoded KiCad version lists found outside kicad_version.py "
        "(AR1 violation). Route these through probe_order()/the resolver:\n"
        + "\n".join(offenders)
    )


def test_guard_would_catch_a_regression():
    # Sanity: the pattern actually matches a real version list.
    assert VERSION_LIST_RE.search('for v in ["9.0", "8.0", "7.0"]:')
    # …and does not flag a lone version string or a version-derived path.
    assert not VERSION_LIST_RE.search('version: str = "9.0"')
    assert not VERSION_LIST_RE.search('Path(base) / "KiCad" / version')
