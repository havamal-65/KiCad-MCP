"""Courtyard shape harvester — one parser for every courtyard graphic (RW-S3).

``courtyard_points`` walks a single footprint block's graphic sub-blocks on the
courtyard layers and returns the point cloud whose axis-aligned bbox bounds the
courtyard. Shared by the gate parser (``tools/drc._parse_placed_courtyards``)
and the auto_place engine (``utils/placement_engine._parse_courtyard_box``) so
circle/poly/arc/curve courtyards are visible to both (spec-courtyard-geometry
G1; the pre-RW-S3 parsers read only ``fp_rect``/``fp_line`` — F-ECC1).

Point contributions per shape:

- ``fp_rect`` / ``fp_line`` — ``(start …)`` and ``(end …)``.
- ``fp_poly`` — every ``(xy …)`` vertex.
- ``fp_circle`` — ``(cx−r, cy−r)`` and ``(cx+r, cy+r)`` with
  ``r = dist(center, end)`` — the exact bbox.
- ``fp_arc`` — both endpoints, plus each axis-extreme point of the containing
  circle whose angle lies within the arc sweep (center/radius recovered from
  start/mid/end by circumcircle) — the exact arc bbox. Degenerate/collinear
  triples fall back to the three raw points.
- ``fp_curve`` (Bézier) — its four control points (conservative superset — a
  Bézier lies within its control hull).

Pure text parsing; no pcbnew.
"""

from __future__ import annotations

import math
import re

from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

_START_END_PAT = re.compile(r"\((?:start|end)\s+([-\d.]+)\s+([-\d.]+)\)")
_START_PAT = re.compile(r"\(start\s+([-\d.]+)\s+([-\d.]+)\)")
_MID_PAT = re.compile(r"\(mid\s+([-\d.]+)\s+([-\d.]+)\)")
_END_PAT = re.compile(r"\(end\s+([-\d.]+)\s+([-\d.]+)\)")
_CENTER_PAT = re.compile(r"\(center\s+([-\d.]+)\s+([-\d.]+)\)")
_XY_PAT = re.compile(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)")

_SHAPE_TOKENS = ("fp_rect", "fp_line", "fp_poly", "fp_circle", "fp_arc",
                 "fp_curve")

_TWO_PI = 2.0 * math.pi


def _ccw_delta(a: float, b: float) -> float:
    """Angular distance from ``a`` to ``b`` going counter-clockwise (radians,
    in ``[0, 2π)``)."""
    return (b - a) % _TWO_PI


def _circumcircle(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float, float] | None:
    """Center and radius of the circle through three points, or None when the
    points are (near-)collinear."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return (ux, uy, math.hypot(ax - ux, ay - uy))


def _circle_points(sub: str) -> list[tuple[float, float]]:
    center_m = _CENTER_PAT.search(sub)
    end_m = _END_PAT.search(sub)
    if not center_m or not end_m:
        return []
    cx, cy = float(center_m.group(1)), float(center_m.group(2))
    ex, ey = float(end_m.group(1)), float(end_m.group(2))
    r = math.hypot(ex - cx, ey - cy)
    return [(cx - r, cy - r), (cx + r, cy + r)]


def _arc_points(sub: str) -> list[tuple[float, float]]:
    start_m = _START_PAT.search(sub)
    mid_m = _MID_PAT.search(sub)
    end_m = _END_PAT.search(sub)
    if not start_m or not end_m:
        return []
    p_start = (float(start_m.group(1)), float(start_m.group(2)))
    p_end = (float(end_m.group(1)), float(end_m.group(2)))
    if not mid_m:
        return [p_start, p_end]
    p_mid = (float(mid_m.group(1)), float(mid_m.group(2)))

    circle = _circumcircle(p_start, p_mid, p_end)
    if circle is None:
        return [p_start, p_mid, p_end]
    cx, cy, r = circle

    a_start = math.atan2(p_start[1] - cy, p_start[0] - cx)
    a_mid = math.atan2(p_mid[1] - cy, p_mid[0] - cx)
    a_end = math.atan2(p_end[1] - cy, p_end[0] - cx)

    # Orient the sweep to run counter-clockwise (in file coordinates) from
    # ``a_from`` to ``a_to`` such that it passes through the mid point; the
    # arc's direction on screen is irrelevant to its bbox.
    if _ccw_delta(a_start, a_mid) <= _ccw_delta(a_start, a_end):
        a_from, a_to = a_start, a_end
    else:
        a_from, a_to = a_end, a_start
    span = _ccw_delta(a_from, a_to)

    points = [p_start, p_end]
    for angle, extreme in (
        (0.0, (cx + r, cy)),
        (math.pi / 2.0, (cx, cy + r)),
        (math.pi, (cx - r, cy)),
        (-math.pi / 2.0, (cx, cy - r)),
    ):
        if _ccw_delta(a_from, angle) <= span:
            points.append(extreme)
    return points


def courtyard_points(
    block: str, layers: tuple[str, ...] = ("F.CrtYd", "B.CrtYd"),
) -> list[tuple[float, float]]:
    """Harvest the courtyard point cloud from one footprint block.

    Returns footprint-local points from every graphic sub-block on ``layers``
    (raw-substring layer match, e.g. ``'"F.CrtYd"' in sub_block``); the caller
    applies rotation and takes min/max for the bbox. Empty list when the block
    carries no courtyard geometry.
    """
    needles = tuple(f'"{layer}"' for layer in layers)
    points: list[tuple[float, float]] = []
    i = 0
    n = len(block)
    while i < n:
        if block[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and block[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = block[i + 1 : j]
        if token in _SHAPE_TOKENS:
            end_idx = _walk_balanced_parens(block, i)
            if end_idx is None:
                i += 1
                continue
            sub = block[i : end_idx + 1]
            if any(needle in sub for needle in needles):
                if token in ("fp_rect", "fp_line"):
                    for m in _START_END_PAT.finditer(sub):
                        points.append((float(m.group(1)), float(m.group(2))))
                elif token in ("fp_poly", "fp_curve"):
                    for m in _XY_PAT.finditer(sub):
                        points.append((float(m.group(1)), float(m.group(2))))
                elif token == "fp_circle":
                    points.extend(_circle_points(sub))
                else:  # fp_arc
                    points.extend(_arc_points(sub))
            i = end_idx + 1
            continue
        i += 1
    return points
