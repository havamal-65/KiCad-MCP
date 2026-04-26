"""Catalog of well-known third-party KiCad library sources.

This is a static registry of the major sources we know how to ingest. A
source is either:

* ``git``  — a public git repository that ships ``.kicad_sym`` / ``.pretty``
  files. ``bootstrap_known_source`` clones it and registers it.
* ``api``  — a per-part fetch source backed by an HTTP API (SnapMagic,
  Ultra Librarian, Octopart). Parts are downloaded on demand and cached
  in ``~/.kicad-mcp/external_libs/<source>/``.
* ``web``  — a manual download site (PCB Libraries Pro). The catalog
  records the homepage so the MCP can tell the user where to go.

The catalog deliberately lives in code, not a database, so it ships with
the package and stays correct across upgrades.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class KnownSource:
    """A third-party KiCad library source we know how to handle."""

    name: str
    kind: str  # "git", "api", or "web"
    description: str
    url: str
    license: str = "see source"
    requires_auth: bool = False
    auth_env_var: str | None = None
    homepage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Bulk-mirror sources (git clone, then index locally)
# ---------------------------------------------------------------------------

_GIT_SOURCES: list[KnownSource] = [
    KnownSource(
        name="kicad-symbols",
        kind="git",
        description="Official KiCad symbol library (already shipped with KiCad, "
                    "register only if you want a separate checkout).",
        url="https://gitlab.com/kicad/libraries/kicad-symbols.git",
        license="CC-BY-SA-4.0",
        homepage="https://kicad.org/libraries/",
    ),
    KnownSource(
        name="kicad-footprints",
        kind="git",
        description="Official KiCad footprint library.",
        url="https://gitlab.com/kicad/libraries/kicad-footprints.git",
        license="CC-BY-SA-4.0",
        homepage="https://kicad.org/libraries/",
    ),
    KnownSource(
        name="digikey",
        kind="git",
        description="Digi-Key's atomic KiCad library — symbols, footprints, "
                    "and 3D models for parts in their catalog.",
        url="https://github.com/Digi-Key/digikey-kicad-library.git",
        license="CC-BY-SA-4.0",
        homepage="https://github.com/Digi-Key/digikey-kicad-library",
    ),
    KnownSource(
        name="sparkfun",
        kind="git",
        description="SparkFun's KiCad libraries (symbols + footprints). "
                    "Also available through the KiCad PCM since fall 2025; "
                    "the PCM install honours KICAD9_3RD_PARTY.",
        url="https://github.com/sparkfun/SparkFun-KiCad-Libraries.git",
        license="CC-BY-SA-4.0",
        homepage="https://github.com/sparkfun/SparkFun-KiCad-Libraries",
    ),
    KnownSource(
        name="wurth-elektronik",
        kind="git",
        description="Würth Elektronik symbols, footprints, and 3D models.",
        url="https://github.com/WurthElektronik/KiCAD-Library.git",
        license="see source",
        homepage="https://www.we-online.com/en/support/cad-models",
    ),
]


# ---------------------------------------------------------------------------
# API-backed sources (search → fetch → cache locally)
# ---------------------------------------------------------------------------

_API_SOURCES: list[KnownSource] = [
    KnownSource(
        name="snapmagic",
        kind="api",
        description="SnapMagic Search (formerly SnapEDA) — millions of parts, "
                    "free symbols/footprints/3D models, IPC-7351B compliant.",
        url="https://www.snapeda.com/api/v1/",
        license="per-part, see SnapMagic terms",
        requires_auth=True,
        auth_env_var="SNAPMAGIC_API_KEY",
        homepage="https://www.snapeda.com/",
    ),
    KnownSource(
        name="ultra-librarian",
        kind="api",
        description="Ultra Librarian — distributor-integrated, official KiCad "
                    "partnership for version compatibility.",
        url="https://app.ultralibrarian.com/api/",
        license="per-part, see Ultra Librarian terms",
        requires_auth=True,
        auth_env_var="ULTRA_LIBRARIAN_API_KEY",
        homepage="https://www.ultralibrarian.com/",
    ),
    KnownSource(
        name="octopart",
        kind="api",
        description="Octopart Nexar GraphQL API — component metadata + "
                    "symbol/footprint URLs across distributors.",
        url="https://api.nexar.com/graphql/",
        license="per-part, see Nexar terms",
        requires_auth=True,
        auth_env_var="NEXAR_API_TOKEN",
        homepage="https://octopart.com/api/v4/",
    ),
]


# ---------------------------------------------------------------------------
# Web download sources (no automation possible — record where to go)
# ---------------------------------------------------------------------------

_WEB_SOURCES: list[KnownSource] = [
    KnownSource(
        name="pcb-libraries-pro",
        kind="web",
        description="PCB Libraries Professional Edition — free for KiCad "
                    "users. Manual download from vendor site.",
        url="https://www.pcblibraries.com/",
        license="free for KiCad users (vendor terms)",
        homepage="https://www.pcblibraries.com/",
    ),
]


_ALL: list[KnownSource] = _GIT_SOURCES + _API_SOURCES + _WEB_SOURCES
_BY_NAME: dict[str, KnownSource] = {s.name: s for s in _ALL}


def list_known_sources() -> list[dict[str, Any]]:
    """Return every catalogued source as a list of dicts."""
    return [s.to_dict() for s in _ALL]


def get_known_source(name: str) -> KnownSource | None:
    """Look up a source by catalog name (case-insensitive)."""
    return _BY_NAME.get(name.lower())


def known_source_names() -> list[str]:
    return [s.name for s in _ALL]
