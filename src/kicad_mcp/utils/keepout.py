"""Keep-out (rule area) parsing + polygon geometry for the placement gate (K1).

Reads every keep-out area — board-level ``(zone … (keepout …))`` rule areas and
footprint-embedded keep-out zones — from ``.kicad_pcb`` text and tests courtyard
rectangles against the keep-out polygons with true polygon geometry.

Zone polygon points in a ``.kicad_pcb`` are **board-absolute** for embedded
zones too (KiCad stores FP_ZONE outlines in board space and rewrites them on
move/rotate), so no coordinate transform is applied here. In a ``.kicad_mod``
library file the same points are footprint-local; the file backend transforms
them at placement time (REQ-KWRITE-001).

Pure text/math — no KiCad, bridge, or file-system dependency.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

#: The five per-object rules a ``(keepout …)`` block can carry.
KEEPOUT_OBJECT_RULES: tuple[str, ...] = (
    "tracks", "vias", "pads", "copperpour", "footprints",
)

#: Compound / wildcard copper-layer tokens resolved to explicit sides. Other
#: tokens (e.g. ``In1.Cu`` on a 4-layer board) are carried through verbatim.
_LAYER_TOKEN_MAP: dict[str, tuple[str, ...]] = {
    "*.Cu": ("F.Cu", "B.Cu"),
    "F&B.Cu": ("F.Cu", "B.Cu"),
}

_QUOTED_PAT = re.compile(r'"([^"]*)"')
_NAME_PAT = re.compile(r'\(name\s+"([^"]*)"\)')
_XY_PAT = re.compile(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)")
_ARC_PAT = re.compile(
    r"\(arc\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s*"
    r"\(mid\s+([-\d.]+)\s+([-\d.]+)\)\s*"
    r"\(end\s+([-\d.]+)\s+([-\d.]+)\)\s*\)"
)
_REF_PAT = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
_FP_REF_PAT = re.compile(r'\(fp_text\s+reference\s+"([^"]+)"')

Point = tuple[float, float]
Polygon = tuple[Point, ...]


@dataclass(frozen=True)
class KeepoutArea:
    """One keep-out rule area read from a board file."""

    origin: str                    # "board" or "embedded:<ref>"
    name: str | None               # zone (name "…"), for reporting
    layers: frozenset[str]         # resolved copper layers, e.g. {"F.Cu", "B.Cu"}
    not_allowed: frozenset[str]    # subset of KEEPOUT_OBJECT_RULES
    polygons: tuple[Polygon, ...]  # board-mm outlines, arcs flattened

    @property
    def forbids_footprints(self) -> bool:
        return "footprints" in self.not_allowed


# ---------------------------------------------------------------------------
# Geometry (REQ-KGEOM-*)
# ---------------------------------------------------------------------------

def flatten_arc(
    start: Point, mid: Point, end: Point, max_dev: float,
) -> tuple[Point, ...]:
    """Flatten a 3-point arc to a polyline with chord deviation ≤ ``max_dev``.

    Sagitta-bounded subdivision (REQ-KGEOM-004). Degenerate arcs (collinear
    points, tiny radius, or zero sweep) fall back to the three points as
    straight segments. Endpoints are emitted exactly — no drift.
    """
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return (start, mid, end)
    a_sq = ax * ax + ay * ay
    b_sq = bx * bx + by * by
    c_sq = cx * cx + cy * cy
    ux = (a_sq * (by - cy) + b_sq * (cy - ay) + c_sq * (ay - by)) / d
    uy = (a_sq * (cx - bx) + b_sq * (ax - cx) + c_sq * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    if radius <= max_dev:
        return (start, mid, end)

    two_pi = 2.0 * math.pi
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    sweep_ccw = (a2 - a0) % two_pi
    mid_ccw = (a1 - a0) % two_pi
    # The sweep runs whichever way passes through the mid point.
    sweep = sweep_ccw if mid_ccw <= sweep_ccw else sweep_ccw - two_pi
    if abs(sweep) < 1e-12:
        return (start, mid, end)

    step = 2.0 * math.acos(max(1.0 - max_dev / radius, -1.0))
    if step <= 0.0:
        return (start, mid, end)
    nseg = max(2, math.ceil(abs(sweep) / step))
    points: list[Point] = [start]
    for i in range(1, nseg):
        angle = a0 + sweep * (i / nseg)
        points.append((ux + radius * math.cos(angle), uy + radius * math.sin(angle)))
    points.append(end)
    return tuple(points)


def point_in_polygon(pt: Point, poly: Polygon) -> bool:
    """Even-odd ray-casting point-in-polygon, correct for concave outlines
    (REQ-KGEOM-002). Half-open edge rule keeps vertex crossings stable."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_properly_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True iff segment ab strictly crosses segment cd (touching does not
    count — the tolerance shrink in ``rect_intersects_polygon`` owns boundary
    semantics)."""
    d1 = _orient(c, d, a)
    d2 = _orient(c, d, b)
    d3 = _orient(a, b, c)
    d4 = _orient(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and (
        d1 != 0 or d2 != 0
    ) and (d3 != 0 or d4 != 0)


def rect_intersects_polygon(
    rect: tuple[float, float, float, float], poly: Polygon, tol: float,
) -> bool:
    """True iff the rect and polygon share positive area (REQ-KGEOM-001/-003).

    ``rect`` is ``(xmin, ymin, xmax, ymax)``. Positive-area semantics come from
    contracting the rect by ``tol`` per side before testing, so an
    exactly-touching boundary never flags. Intersection iff any shrunk-rect
    corner lies in the polygon (covers rect-fully-inside), any polygon vertex
    lies in the shrunk rect (covers polygon-fully-inside-rect), or any edge
    pair properly crosses (covers partial overlaps incl. concave fingers).
    """
    if len(poly) < 3:
        return False
    xmin, ymin, xmax, ymax = rect
    xmin += tol
    ymin += tol
    xmax -= tol
    ymax -= tol
    if xmin >= xmax or ymin >= ymax:
        return False

    corners: tuple[Point, ...] = (
        (xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax),
    )
    if any(point_in_polygon(c, poly) for c in corners):
        return True
    if any(xmin < px < xmax and ymin < py < ymax for px, py in poly):
        return True
    n = len(poly)
    for i in range(4):
        r1 = corners[i]
        r2 = corners[(i + 1) % 4]
        for j in range(n):
            if _segments_properly_cross(r1, r2, poly[j], poly[(j + 1) % n]):
                return True
    return False


# ---------------------------------------------------------------------------
# Polygon transforms (K2 — REQ-KAVOID-003)
# ---------------------------------------------------------------------------

def transform_polygon(poly: Polygon, x: float, y: float, rot_deg: float) -> Polygon:
    """Owner-local outline → board frame: rotate by ``rot_deg``, then translate
    by ``(x, y)`` — the KWRITE/courtyard rotation convention (live-verified)."""
    if rot_deg == 0.0:
        return tuple((px + x, py + y) for px, py in poly)
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)
    return tuple((px * c - py * s + x, px * s + py * c + y) for px, py in poly)


def untransform_polygon(poly: Polygon, x: float, y: float, rot_deg: float) -> Polygon:
    """Inverse of :func:`transform_polygon`: board frame → owner-local, so a
    movable footprint's embedded keep-out can be re-transformed to any
    candidate placement (REQ-KAVOID-003)."""
    if rot_deg == 0.0:
        return tuple((px - x, py - y) for px, py in poly)
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)
    return tuple(
        ((px - x) * c + (py - y) * s, -(px - x) * s + (py - y) * c)
        for px, py in poly
    )


# ---------------------------------------------------------------------------
# Parsing (REQ-KPARSE-*)
# ---------------------------------------------------------------------------

def _resolve_layer_tokens(tokens: list[str]) -> frozenset[str]:
    resolved: set[str] = set()
    for token in tokens:
        resolved.update(_LAYER_TOKEN_MAP.get(token, (token,)))
    return frozenset(resolved)


def _parse_zone_layers(zone_header: str) -> frozenset[str]:
    """Resolve a zone's layer clause from its header text (the block up to the
    first outline polygon, so ``filled_polygon``-level layer clauses cannot
    interfere). Accepts ``(layers "…" …)`` and single ``(layer "…")``."""
    m = re.search(r"\(layers\s+([^)]*)\)", zone_header)
    if m:
        return _resolve_layer_tokens(_QUOTED_PAT.findall(m.group(1)))
    m = re.search(r'\(layer\s+"([^"]+)"\)', zone_header)
    if m:
        return _resolve_layer_tokens([m.group(1)])
    return frozenset()


def _find_sub_blocks(block: str, token: str) -> list[tuple[int, str]]:
    """Return ``(start_offset, text)`` of every ``(<token> …)`` sub-block."""
    out: list[tuple[int, str]] = []
    pat = re.compile(r"\(" + token + r"[\s(]")
    pos = 0
    while True:
        m = pat.search(block, pos)
        if m is None:
            return out
        end = _walk_balanced_parens(block, m.start())
        if end is None:
            return out
        out.append((m.start(), block[m.start():end + 1]))
        pos = end + 1


def _parse_outline_points(polygon_block: str, arc_max_dev: float) -> Polygon:
    """Vertices of one ``(polygon (pts …))`` block, arcs flattened, in file
    order. Consecutive duplicate points (arc endpoints meeting ``xy`` tokens)
    are collapsed."""
    points: list[Point] = []
    # Walk pts children in file order: xy and arc tokens interleave.
    token_pat = re.compile(r"\((?:xy|arc)[\s(]")
    pos = 0
    while True:
        m = token_pat.search(polygon_block, pos)
        if m is None:
            break
        end = _walk_balanced_parens(polygon_block, m.start())
        if end is None:
            break
        chunk = polygon_block[m.start():end + 1]
        if chunk.startswith("(xy"):
            xym = _XY_PAT.match(chunk)
            if xym:
                points.append((float(xym.group(1)), float(xym.group(2))))
        else:
            arcm = _ARC_PAT.match(chunk)
            if arcm:
                vals = [float(g) for g in arcm.groups()]
                flat = flatten_arc(
                    (vals[0], vals[1]), (vals[2], vals[3]), (vals[4], vals[5]),
                    arc_max_dev,
                )
                for p in flat:
                    if not points or points[-1] != p:
                        points.append(p)
        pos = end + 1
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return tuple(points)


def _parse_zone_block(
    zone_block: str, origin: str, arc_max_dev: float, warnings: list[str],
) -> KeepoutArea | None:
    """Parse one ``(zone …)`` block into a KeepoutArea, or ``None`` for copper
    pours / malformed zones (REQ-KPARSE-003/-005)."""
    keepout_blocks = _find_sub_blocks(zone_block, "keepout")
    if not keepout_blocks:
        return None  # copper pour, not a rule area

    keepout_body = keepout_blocks[0][1]
    not_allowed = frozenset(
        rule for rule in KEEPOUT_OBJECT_RULES
        if re.search(r"\(" + rule + r"\s+not_allowed\)", keepout_body)
    )

    polygon_blocks = _find_sub_blocks(zone_block, "polygon")
    header_end = polygon_blocks[0][0] if polygon_blocks else len(zone_block)
    fill_blocks = _find_sub_blocks(zone_block, "filled_polygon")
    if fill_blocks:
        header_end = min(header_end, fill_blocks[0][0])
    header = zone_block[:header_end]

    name_m = _NAME_PAT.search(header)
    layers = _parse_zone_layers(header)

    polygons: list[Polygon] = []
    for _, poly_block in polygon_blocks:
        outline = _parse_outline_points(poly_block, arc_max_dev)
        if len(outline) >= 3:
            polygons.append(outline)
        else:
            warnings.append(
                f"keep-out zone ({origin}) has a degenerate outline "
                f"(<3 vertices) — outline skipped"
            )
    if not polygons:
        warnings.append(
            f"keep-out zone ({origin}) has no usable outline polygon — skipped"
        )
        return None

    return KeepoutArea(
        origin=origin,
        name=name_m.group(1) if name_m else None,
        layers=layers,
        not_allowed=not_allowed,
        polygons=tuple(polygons),
    )


def scan_board(
    content: str,
) -> tuple[list[KeepoutArea], dict[str, str], list[str]]:
    """One linear pass over board text (REQ-KPARSE-001).

    Returns ``(keepouts, footprint_sides, warnings)`` where ``footprint_sides``
    maps each real ref (non-``#``) to its footprint's own layer
    (``F.Cu``/``B.Cu``, from the block header line).
    """
    from kicad_mcp.utils.placement_config import get_float

    arc_max_dev = get_float("ARC_MAX_DEVIATION_MM")
    keepouts: list[KeepoutArea] = []
    sides: dict[str, str] = {}
    warnings: list[str] = []

    i = 0
    n = len(content)
    while i < n:
        if content[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and content[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = content[i + 1:j]

        if token == "zone":
            end = _walk_balanced_parens(content, i)
            if end is None:
                i += 1
                continue
            area = _parse_zone_block(
                content[i:end + 1], "board", arc_max_dev, warnings,
            )
            if area is not None:
                keepouts.append(area)
            i = end + 1
            continue

        if token == "footprint":
            end = _walk_balanced_parens(content, i)
            if end is None:
                i += 1
                continue
            block = content[i:end + 1]
            ref_m = _REF_PAT.search(block) or _FP_REF_PAT.search(block)
            ref = ref_m.group(1) if ref_m else None
            if ref and not ref.startswith("#"):
                side_m = re.search(r'\(layer\s+"([^"]+)"\)', block)
                sides[ref] = side_m.group(1) if side_m else "F.Cu"
            origin = f"embedded:{ref}" if ref else "embedded:?"
            for _, zone_block in _find_sub_blocks(block, "zone"):
                area = _parse_zone_block(zone_block, origin, arc_max_dev, warnings)
                if area is not None:
                    keepouts.append(area)
            i = end + 1
            continue

        i += 1

    return keepouts, sides, warnings


def parse_keepouts(content: str) -> tuple[list[KeepoutArea], list[str]]:
    """Keep-out areas + warnings from board text (thin over ``scan_board``)."""
    keepouts, _, warnings = scan_board(content)
    return keepouts, warnings


def parse_footprint_sides(content: str) -> dict[str, str]:
    """Ref → footprint side (``F.Cu``/``B.Cu``) map (thin over ``scan_board``)."""
    _, sides, _ = scan_board(content)
    return sides


# ---------------------------------------------------------------------------
# Intrusion check (feeds REQ-KGATE-001…003)
# ---------------------------------------------------------------------------

def find_keepout_intrusions(
    courtyards: dict[str, dict[str, float]],
    sides: dict[str, str],
    keepouts: list[KeepoutArea],
) -> list[dict[str, Any]]:
    """Blocking ``keepout_intrusion`` violation dicts for every courtyard rect
    intersecting a footprint-forbidding keep-out on a matching layer.

    Deterministic: keep-outs in file order, refs sorted. A footprint is never
    tested against its own embedded keep-out (A3 self-exemption).
    """
    from kicad_mcp.utils.placement_config import get_float

    tol = get_float("KEEPOUT_EDGE_TOL_MM")
    violations: list[dict[str, Any]] = []
    for area in keepouts:
        if not area.forbids_footprints:
            continue
        for ref in sorted(courtyards):
            if area.origin == f"embedded:{ref}":
                continue
            side = sides.get(ref, "F.Cu")
            if side not in area.layers:
                continue
            c = courtyards[ref]
            rect = (c["xmin"], c["ymin"], c["xmax"], c["ymax"])
            if any(
                rect_intersects_polygon(rect, poly, tol)
                for poly in area.polygons
            ):
                origin_phrase = (
                    "a board-level keep-out area" if area.origin == "board"
                    else f"a keep-out area embedded in "
                         f"{area.origin.split(':', 1)[1]}"
                )
                violations.append({
                    "type": "keepout_intrusion",
                    "severity": "blocking",
                    "reference": ref,
                    "keepout_origin": area.origin,
                    "keepout_name": area.name,
                    "layers": sorted(area.layers & {side}) or [side],
                    "detail": (
                        f"{ref}'s courtyard intrudes into {origin_phrase} "
                        f"(footprints not allowed)."
                    ),
                })
    return violations
