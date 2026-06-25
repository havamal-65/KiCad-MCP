"""Manufacturing-tolerance model shared by estimate_board_size and
verify_board_size (§6.5).

One source of truth for the tolerance defaults (resolves HLRP §10 Q3) so the
estimator's recommendation is one the verifier will accept. Every value is
overridable per call; these are the conservative typical-fab defaults.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BoardSizeTolerances:
    """Manufacturing tolerances for board sizing. All distances in mm."""

    panel_keepout_mm: float = 3.0          # V-score / panel-rail inset, all 4 edges
    mounting_hole_keepout_mm: float = 3.0  # radius from each mounting-hole centre
    fiducial_keepout_mm: float = 1.0       # radius from each fiducial centre
    routing_channel_pct: float = 20.0      # % of courtyard area reserved for routing
    utilization_ceiling_pct: float = 80.0  # warn above this required/usable ratio


DEFAULT_TOLERANCES = BoardSizeTolerances()


def ceil5(v: float) -> float:
    """Round up to the nearest 5 mm (standard fab panel snap)."""
    return math.ceil(v / 5.0) * 5.0


# Footprint classification (per the §6.5 requirements §2 definitions). A mounting
# hole or fiducial contributes a keepout disc that reduces usable board area.
_MH_REF_PAT = re.compile(r"^(MH|H)\d", re.IGNORECASE)
_FID_REF_PAT = re.compile(r"^FID", re.IGNORECASE)


def is_mounting_hole(ref: str, lib_id: str) -> bool:
    """True when a footprint is a mounting hole (ref MH#/H# or MountingHole lib)."""
    return bool(_MH_REF_PAT.match(ref or "")) or "mountinghole" in (lib_id or "").lower()


def is_fiducial(ref: str, lib_id: str) -> bool:
    """True when a footprint is a fiducial (ref FID* or Fiducial lib)."""
    return bool(_FID_REF_PAT.match(ref or "")) or "fiducial" in (lib_id or "").lower()


def suggest_dimensions(
    required_area_mm2: float,
    max_part_w_mm: float,
    max_part_h_mm: float,
    mh_disc_area_mm2: float,
    fid_disc_area_mm2: float,
    tol: BoardSizeTolerances,
    aspect: float,
) -> dict[str, float]:
    """Smallest (width, height) at *aspect* (=w/h) whose usable area covers
    *required_area_mm2* and that fits the single largest part. Rounded up to 5 mm.

    usable(W,H) = (W - 2k)(H - 2k) - mh_discs - fid_discs, so we need the inner
    rectangle to cover required + the disc keepouts.
    """
    k = tol.panel_keepout_mm
    need_inner = required_area_mm2 + mh_disc_area_mm2 + fid_disc_area_mm2
    aspect = aspect if aspect > 0 else 1.4

    # Dimensional floor: the single largest part must physically fit.
    h = max(max_part_h_mm + 2 * k, (max_part_w_mm + 2 * k) / aspect, 1.0)
    # Grow H (W tracks it via aspect) until the inner area covers need_inner.
    for _ in range(100000):
        w = aspect * h
        inner = max(0.0, (w - 2 * k)) * max(0.0, (h - 2 * k))
        if inner >= need_inner and w >= max_part_w_mm + 2 * k:
            break
        h += 0.5
    return {"width_mm": ceil5(aspect * h), "height_mm": ceil5(h)}
