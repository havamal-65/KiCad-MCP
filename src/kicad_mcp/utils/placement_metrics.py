"""Placement-quality metric — the measuring stick (Sprint P1).

Pure, deterministic, read-only. Given a ``.kicad_pcb`` path, this module reads
the pad->net connectivity already on the board (post ``sync_schematic_to_pcb``),
builds a weighted part graph, and computes a placement-quality bundle whose
headline number is **Total HPWL** over signal nets.

Nothing here mutates a board or any file (REQ-METRIC-007). The connectivity is
read from the *placed board* (not the schematic) so it needs no schematic and
reflects exactly what the router will see (spec-p1 §2.1).

Determinism contract (REQ-DET-001): no ``random`` / ``time`` / ``datetime``, no
set-iteration-order dependence in any output (everything is sorted before emit),
all emitted floats rounded to 4 dp so equality is exact across runs.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, TypedDict

from kicad_mcp.utils.placement_config import (
    MAX_NET_FANOUT,
    ORIENT_ROTATION_QUANTUM_DEG,
    classify_net,
)

# Reused, board-frame-aware regexes (mirror drc.py:408-411).
_AT_PAT = re.compile(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)")
_REF_PAT = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
_FP_REF_PAT = re.compile(r'\(fp_text\s+reference\s+"([^"]+)"')
_FP_LIBID_PAT = re.compile(r'\(footprint\s+"([^"]+)"')
_PAD_NAME_PAT = re.compile(r'\(pad\s+(?:"([^"]*)"|([^\s()]+))')
_NET_PAT = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')


class PadRecord(TypedDict):
    """A single placed pad carrying a real net, in board-frame coordinates."""

    ref: str
    pad: str
    net_id: int
    net_name: str
    x_mm: float
    y_mm: float


class _Footprint(TypedDict):
    ref: str
    lib_id: str
    rot_deg: float
    pads: list[PadRecord]


def _parse_footprints(content: str) -> list[_Footprint]:
    """Walk every real footprint block, returning ref / lib_id / rotation / pads.

    Reuses the balanced-paren footprint-walking skeleton proven in
    ``_parse_placed_courtyards`` (drc.py:389-490). Refs starting with ``#``
    (power/mechanical pseudo-refs) are skipped, matching the courtyard parser.
    Only pads carrying a non-zero, non-empty net are recorded as PadRecords
    (REQ-GRAPH-002).
    """
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    footprints: list[_Footprint] = []
    i = 0
    n = len(content)
    while i < n:
        if content[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and content[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = content[i + 1 : j]

        if token != "footprint":
            i += 1
            continue

        end_idx = _walk_balanced_parens(content, i)
        if end_idx is None:
            i += 1
            continue
        block = content[i : end_idx + 1]

        ref_m = _REF_PAT.search(block) or _FP_REF_PAT.search(block)
        ref = ref_m.group(1) if ref_m else None
        if not ref or ref.startswith("#"):
            i = end_idx + 1
            continue

        libid_m = _FP_LIBID_PAT.match(block)
        lib_id = libid_m.group(1) if libid_m else ""

        # Footprint origin (at fx fy [frot]). Scan only the header region so a
        # pad's inner (at ...) cannot be mistaken for the footprint origin
        # (mirror drc.py:439).
        header = block[: block.find("\n") + 200] if "\n" in block else block
        at_m = _AT_PAT.search(header)
        fx = float(at_m.group(1)) if at_m else 0.0
        fy = float(at_m.group(2)) if at_m else 0.0
        frot = float(at_m.group(3)) if (at_m and at_m.group(3)) else 0.0

        rad = math.radians(frot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)

        pads: list[PadRecord] = []
        search_start = 0
        while True:
            pidx = block.find("(pad ", search_start)
            if pidx == -1:
                break
            pend = _walk_balanced_parens(block, pidx)
            if pend is None:
                search_start = pidx + 1
                continue
            pad_block = block[pidx : pend + 1]
            search_start = pend + 1

            net_m = _NET_PAT.search(pad_block)
            if not net_m:
                continue
            net_id = int(net_m.group(1))
            net_name = net_m.group(2)
            # Net 0 / empty name => mechanical / unconnected (REQ-GRAPH-002).
            if net_id == 0 or not net_name:
                continue

            name_m = _PAD_NAME_PAT.match(pad_block)
            pad_name = ""
            if name_m:
                pad_name = (
                    name_m.group(1) if name_m.group(1) is not None
                    else name_m.group(2)
                )

            at_pm = _AT_PAT.search(pad_block)
            px = float(at_pm.group(1)) if at_pm else 0.0
            py = float(at_pm.group(2)) if at_pm else 0.0

            # Board-frame pad centre: footprint origin + footprint-rotated pad
            # offset. KiCad convention: positive rotation is CCW on screen =
            # CW in file coords (y down), so (x,y) -> (x*c + y*s, -x*s + y*c).
            # Verified against pcbnew-written pads at rot 90 (aqs_v2 J1.A12;
            # tests/test_rotation_convention.py). The pad's own rotation
            # rotates the pad shape only, not its centre offset.
            x_mm = fx + (px * cos_r + py * sin_r)
            y_mm = fy + (-px * sin_r + py * cos_r)

            pads.append(PadRecord(
                ref=ref,
                pad=pad_name,
                net_id=net_id,
                net_name=net_name,
                x_mm=x_mm,
                y_mm=y_mm,
            ))

        footprints.append(_Footprint(
            ref=ref, lib_id=lib_id, rot_deg=frot, pads=pads,
        ))
        i = end_idx + 1

    return footprints


def read_board_pads(content: str) -> list[PadRecord]:
    """Return every placed pad carrying a real net (REQ-GRAPH-001/002).

    Flattens ``_parse_footprints``. Pads on net 0 or with an empty net name are
    excluded (mechanical / unconnected).
    """
    pads: list[PadRecord] = []
    for fp in _parse_footprints(content):
        pads.extend(fp["pads"])
    return pads


def build_net_pads(pads: list[PadRecord]) -> dict[str, list[PadRecord]]:
    """Group pads by net name; sort each net's list by (ref, pad) (REQ-DET-001)."""
    by_net: dict[str, list[PadRecord]] = {}
    for pad in pads:
        by_net.setdefault(pad["net_name"], []).append(pad)
    for net_name in by_net:
        by_net[net_name].sort(key=lambda p: (p["ref"], p["pad"]))
    return by_net


def build_part_graph(
    net_pads: dict[str, list[PadRecord]],
    net_weight_mult: dict[str, float] | None = None,
) -> dict[frozenset[str], float]:
    """Weighted footprint-pair graph (REQ-GRAPH-004/005).

    For each **signal** net touching ``m`` distinct footprints:
      - ``m < 2``  -> contributes nothing (net wholly inside one footprint).
      - ``m > MAX_NET_FANOUT`` -> quasi-bus: zero proximity weight.
      - otherwise -> add ``1 / (m - 1)`` to every unordered distinct pair.
    Power/ground nets contribute zero. Deterministic: nets iterated by sorted
    name, pairs by sorted (ref, ref).

    ``net_weight_mult`` (P4, optional) scales a specific net's per-pair
    contribution — a differential pair or clock net contributes more so its
    endpoints pull tight (REQ-SENSE-002/003). Absent / a net not in the mapping
    ⇒ multiplier ``1.0`` (unchanged P1 behaviour).
    """
    mult = net_weight_mult or {}
    edges: dict[frozenset[str], float] = {}
    for net_name in sorted(net_pads):
        if classify_net(net_name) != "signal":
            continue
        refs = sorted({p["ref"] for p in net_pads[net_name]})
        m = len(refs)
        if m < 2 or m > MAX_NET_FANOUT:
            continue
        weight = (1.0 / (m - 1)) * mult.get(net_name, 1.0)
        for a_i in range(m):
            for b_i in range(a_i + 1, m):
                key = frozenset((refs[a_i], refs[b_i]))
                edges[key] = edges.get(key, 0.0) + weight
    return edges


def _total_hpwl(net_pads: dict[str, list[PadRecord]]) -> tuple[float, int]:
    """Total HPWL over signal nets with >=2 pads, and the contributing count."""
    total = 0.0
    signal_net_count = 0
    for net_name in sorted(net_pads):
        if classify_net(net_name) != "signal":
            continue
        pads = net_pads[net_name]
        if len(pads) < 2:
            continue
        xs = [p["x_mm"] for p in pads]
        ys = [p["y_mm"] for p in pads]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
        signal_net_count += 1
    return round(total, 4), signal_net_count


def _orientation_consistency(footprints: list[_Footprint]) -> float:
    """Part-weighted fraction of like footprints sharing the modal rotation.

    Group footprints by lib id; within each family of >=2 members take the
    fraction in the modal rotation (quantised to ``ORIENT_ROTATION_QUANTUM_DEG``)
    and report the part-weighted mean across families. Families with <2 members
    are vacuously consistent. With no family of >=2 members, returns 1.0.
    """
    families: dict[str, list[float]] = {}
    for fp in footprints:
        families.setdefault(fp["lib_id"], []).append(fp["rot_deg"])

    quantum = ORIENT_ROTATION_QUANTUM_DEG
    weighted_sum = 0.0
    total_members = 0
    for lib_id in sorted(families):
        rots = families[lib_id]
        if len(rots) < 2:
            continue
        buckets: dict[float, int] = {}
        for r in rots:
            q = round((r % 360.0) / quantum) * quantum % 360.0
            buckets[q] = buckets.get(q, 0) + 1
        modal = max(buckets.values())
        weighted_sum += modal
        total_members += len(rots)

    if total_members == 0:
        return 1.0
    return round(weighted_sum / total_members, 4)


def placement_metric(board_path: str | Path) -> dict[str, Any]:
    """Compute the placement-quality bundle for a board (REQ-METRIC-001..005).

    Pure read. The returned bundle has a stable shape; ``decap_*`` fields are
    ``null`` in P1 (populated in P2 once part classification lands).
    """
    path = Path(board_path)
    content = path.read_text(encoding="utf-8")

    footprints = _parse_footprints(content)
    pads: list[PadRecord] = []
    for fp in footprints:
        pads.extend(fp["pads"])
    net_pads = build_net_pads(pads)

    total_hpwl_mm, signal_net_count = _total_hpwl(net_pads)
    scored_parts = sum(1 for fp in footprints if fp["pads"])

    warnings: list[str] = []

    # Legality figures (REQ-METRIC-002) — reuse drc.py, do not reimplement.
    # Lazy import: tools.drc imports this module for the placement_quality tool,
    # so a top-level import would cycle.
    from kicad_mcp.tools.drc import (
        _parse_board_bbox,
        _parse_placed_courtyards,
        compute_edge_overhang_exemptions,
        run_check_courtyard_overlaps,
    )

    overlap_count = run_check_courtyard_overlaps(path)["overlap_count"]

    out_of_outline_count: int | None
    edge_overhang_exemptions: list[dict[str, str]] = []
    outline = _parse_board_bbox(content)
    if outline is None:
        out_of_outline_count = None
        warnings.append("no_board_outline: out_of_outline_count not computed")
    else:
        oxmin, oymin, oxmax, oymax = outline
        courtyards, _ = _parse_placed_courtyards(content)
        offenders = [
            ref for ref, cyd in courtyards.items()
            if (
                cyd["xmin"] < oxmin
                or cyd["ymin"] < oymin
                or cyd["xmax"] > oxmax
                or cyd["ymax"] > oymax
            )
        ]
        # #18 (REQ-EDGE-1/2): an edge-anchored connector whose mating face
        # points off-board overhangs LEGALLY (USB4085 class) — exempt it from
        # out_of_outline; every other overhang still counts. Exemptions are
        # reported so nothing is silently waved through (R2).
        exempt: dict[str, str] = {}
        if offenders:
            exempt = compute_edge_overhang_exemptions(
                path, content, outline, courtyards, offenders,
            )
        out_of_outline_count = len(offenders) - len(exempt)
        edge_overhang_exemptions = [
            {"reference": ref, "evidence": evidence}
            for ref, evidence in sorted(exempt.items())
        ]

    orientation_consistency = _orientation_consistency(footprints)

    # Decoupling-cap proximity (P2). Reuse the engine's classifier + pairing on
    # the board's own records so "which cap decouples which IC" is defined once.
    # Lazy import: placement_engine imports this module at top level.
    decap_max_mm: float | None
    decap_mean_mm: float | None
    from kicad_mcp.utils.placement_engine import (
        classify_parts,
        measure_decap_distances,
        pair_decaps,
        read_part_records,
    )

    parts = read_part_records(content)
    roles = classify_parts(parts)
    decap_pairing, _decap_warn = pair_decaps(parts, roles)
    positions = {pt["ref"]: pt["pos"] for pt in parts}
    decap_max_mm, decap_mean_mm = measure_decap_distances(
        parts, roles, decap_pairing, positions,
    )

    bundle: dict[str, Any] = {
        "total_hpwl_mm": total_hpwl_mm,
        "overlap_count": overlap_count,
        "out_of_outline_count": out_of_outline_count,
        "decap_max_mm": decap_max_mm,
        "decap_mean_mm": decap_mean_mm,
        "orientation_consistency": orientation_consistency,
        "signal_net_count": signal_net_count,
        "scored_parts": scored_parts,
    }
    if edge_overhang_exemptions:
        bundle["edge_overhang_exemptions"] = edge_overhang_exemptions
    if warnings:
        bundle["warnings"] = warnings
    return bundle
