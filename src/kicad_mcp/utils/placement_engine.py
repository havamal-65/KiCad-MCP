"""Net-aware constructive placement engine (Sprint P2).

Pure geometry: records in, plan out. No KiCad / bridge / file-system dependency
beyond reading a board's text (``read_part_records``); the placement functions
themselves operate only on plain records so they are unit-testable with no KiCad
install (spec-p2 §2.1).

Pipeline (``plan_placement`` orchestrates):

    classify_parts → pair_decaps → cluster_parts → place → legalize

The engine consumes the P1 net classifier and part-graph weighting from
``placement_metrics`` / ``placement_config`` so "what is a power net" and "how
strongly are two parts connected" are defined in exactly one place.

Determinism contract (REQ-DET-002): no ``random`` / ``time`` / ``datetime``; every
ordering is by sorted, numeric-aware reference / net name / weight-then-ref; the
candidate search uses a fixed grid with strict-decrease acceptance, so the same
input yields byte-identical plans across runs.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from pathlib import Path
from typing import TypedDict

from kicad_mcp.utils.placement_config import (
    classify_net,
    get_float,
    get_int,
    get_str,
    get_tunable,
    is_clock_net,
)
from kicad_mcp.utils.placement_metrics import build_part_graph
from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------

# Roles (REQ-CLASS-001)
ROLE_CONNECTOR = "connector"
ROLE_IC = "ic"
ROLE_DECOUPLING_CAP = "decoupling_cap"
ROLE_CRYSTAL = "crystal"
ROLE_PASSIVE = "passive"
ROLE_OTHER = "other"

#: Reference prefixes treated as ordinary passives when nothing more specific
#: matched (REQ-CLASS-005).
_PASSIVE_PREFIXES = frozenset({"R", "C", "L", "D", "FB"})


class PadLocal(TypedDict):
    """A pad's net + its *local* (unrotated) offset from the footprint origin."""

    pad: str
    net_id: int
    net_name: str
    dx: float
    dy: float


class PartRecord(TypedDict):
    """Everything the engine needs about one footprint, position-independent.

    ``courtyard`` is the local (unrotated) courtyard box ``(xmin, ymin, xmax,
    ymax)`` relative to the footprint origin, or ``None`` if the footprint has no
    courtyard (the caller supplies a default). ``pos`` is the *current*
    board-frame ``(x, y, rot_deg)`` — used only for anchored parts, which keep it.
    """

    ref: str
    lib_id: str
    cluster_key: str
    pad_count: int
    courtyard: tuple[float, float, float, float] | None
    pads: list[PadLocal]
    pos: tuple[float, float, float]


# ---------------------------------------------------------------------------
# Reference-designator helpers (numeric-aware ordering, REQ-DET-002)
# ---------------------------------------------------------------------------

_PREFIX_PAT = re.compile(r"^([A-Za-z]+)(\d+)?")


def _ref_prefix(ref: str) -> str:
    m = _PREFIX_PAT.match(ref)
    return m.group(1).upper() if m else ""


def _ref_key(ref: str) -> tuple[str, int, str]:
    """Numeric-aware sort key so ``U2`` sorts before ``U10`` (REQ-DECAP-002)."""
    m = _PREFIX_PAT.match(ref)
    if not m:
        return (ref.upper(), -1, ref)
    prefix = m.group(1).upper()
    num = int(m.group(2)) if m.group(2) is not None else -1
    return (prefix, num, ref)


def _is_ground(net_name: str) -> bool:
    """Ground rails: the GND/VSS family (a subset of the power class)."""
    up = net_name.strip().upper()
    if not up:
        return False
    return up.startswith("GND") or up in {"VSS", "VSSA", "AGND", "DGND", "PGND"}


# ---------------------------------------------------------------------------
# Board → records (pure text parse; the only board-reading surface)
# ---------------------------------------------------------------------------

_AT_PAT = re.compile(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)")
_REF_PAT = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
_FP_REF_PAT = re.compile(r'\(fp_text\s+reference\s+"([^"]+)"')
_FP_LIBID_PAT = re.compile(r'\(footprint\s+"([^"]+)"')
_PAD_NAME_PAT = re.compile(r'\(pad\s+(?:"([^"]*)"|([^\s()]+))')
_NET_PAT = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_CLUSTERID_PAT = re.compile(r'\(property\s+"ClusterId"\s+"([^"]*)"')
_PATH_PAT = re.compile(r'\(path\s+"([^"]*)"')
_START_PAT = re.compile(r"\(start\s+([-\d.]+)\s+([-\d.]+)\)")
_END_PAT = re.compile(r"\(end\s+([-\d.]+)\s+([-\d.]+)\)")


def _parse_courtyard_box(block: str) -> tuple[float, float, float, float] | None:
    """Local courtyard box from an fp_rect / fp_line on F./B.CrtYd, or None."""
    xs: list[float] = []
    ys: list[float] = []
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
        if token in ("fp_rect", "fp_line", "fp_poly"):
            end_idx = _walk_balanced_parens(block, i)
            if end_idx is None:
                i += 1
                continue
            sub = block[i : end_idx + 1]
            if '"F.CrtYd"' in sub or '"B.CrtYd"' in sub:
                for m in _START_PAT.finditer(sub):
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
                for m in _END_PAT.finditer(sub):
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
                for m in re.finditer(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)", sub):
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
            i = end_idx + 1
            continue
        i += 1
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def read_part_records(content: str) -> list[PartRecord]:
    """Parse every real footprint into a :class:`PartRecord` (board-frame text).

    Pads are recorded with their *local* offset and their net (net 0 / empty
    name kept as net_id 0 so pad_count is the true pad count). Pseudo-refs
    starting with ``#`` are skipped, matching the metric/courtyard parsers.
    """
    records: list[PartRecord] = []
    i = 0
    n = len(content)
    while i < n:
        if content[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and content[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        if content[i + 1 : j] != "footprint":
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

        header = block[: block.find("\n") + 200] if "\n" in block else block
        at_m = _AT_PAT.search(header)
        fx = float(at_m.group(1)) if at_m else 0.0
        fy = float(at_m.group(2)) if at_m else 0.0
        frot = float(at_m.group(3)) if (at_m and at_m.group(3)) else 0.0

        cluster_key = ""
        cid_m = _CLUSTERID_PAT.search(block)
        if cid_m and cid_m.group(1):
            cluster_key = cid_m.group(1)
        else:
            path_m = _PATH_PAT.search(block)
            if path_m and path_m.group(1):
                cluster_key = path_m.group(1)

        pads: list[PadLocal] = []
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

            net_m = _NET_PAT.search(pad_block)
            net_id = int(net_m.group(1)) if net_m else 0
            net_name = net_m.group(2) if net_m else ""

            pads.append(PadLocal(
                pad=pad_name, net_id=net_id, net_name=net_name, dx=px, dy=py,
            ))

        records.append(PartRecord(
            ref=ref,
            lib_id=lib_id,
            cluster_key=cluster_key,
            pad_count=len(pads),
            courtyard=_parse_courtyard_box(block),
            pads=pads,
            pos=(fx, fy, frot),
        ))
        i = end_idx + 1

    return records


# ---------------------------------------------------------------------------
# Net index + part graph
# ---------------------------------------------------------------------------

def build_net_index(parts: list[PartRecord]) -> dict[str, list[tuple[str, float, float]]]:
    """Map net name -> [(ref, dx, dy), ...] over every real-net pad.

    Net 0 / empty-name pads are excluded (mechanical / unconnected). Deterministic
    (refs within a net sorted numeric-aware).
    """
    index: dict[str, list[tuple[str, float, float]]] = {}
    for p in parts:
        for pad in p["pads"]:
            if pad["net_id"] == 0 or not pad["net_name"]:
                continue
            index.setdefault(pad["net_name"], []).append(
                (p["ref"], pad["dx"], pad["dy"])
            )
    for net in index:
        index[net].sort(key=lambda t: (_ref_key(t[0]), t[1], t[2]))
    return index


def _net_weight_multipliers(
    net_names: frozenset[str], diff_pair_nets: frozenset[str],
) -> dict[str, float]:
    """Per-net proximity-weight multipliers for the part graph (P4).

    A declared differential-pair net gets ``DIFFPAIR_WEIGHT_MULT``; a clock-like
    net (``CLOCK_NET_PATTERN``) gets ``CLOCK_WEIGHT_MULT``. Diff-pair wins when a
    net is both. Nets with the default ×1 weight are omitted (REQ-SENSE-002/003).
    """
    diff_mult = get_float("DIFFPAIR_WEIGHT_MULT")
    clock_mult = get_float("CLOCK_WEIGHT_MULT")
    out: dict[str, float] = {}
    for nn in net_names:
        if nn in diff_pair_nets:
            out[nn] = diff_mult
        elif is_clock_net(nn):
            out[nn] = clock_mult
    return out


def _part_graph(
    parts: list[PartRecord],
    diff_pair_nets: frozenset[str] = frozenset(),
) -> dict[frozenset[str], float]:
    """Weighted footprint-pair graph, reusing the P1 weighting (REQ-GRAPH-004).

    ``diff_pair_nets`` (P4) names nets declared as differential pairs; together
    with clock-like nets they receive an extra proximity multiplier so their
    endpoints pull tight (REQ-SENSE-002/003).
    """
    from kicad_mcp.utils.placement_metrics import PadRecord

    net_pads: dict[str, list[PadRecord]] = {}
    for p in parts:
        for pad in p["pads"]:
            if pad["net_id"] == 0 or not pad["net_name"]:
                continue
            net_pads.setdefault(pad["net_name"], []).append(PadRecord(
                ref=p["ref"], pad=pad["pad"], net_id=pad["net_id"],
                net_name=pad["net_name"], x_mm=0.0, y_mm=0.0,
            ))
    mult = _net_weight_multipliers(frozenset(net_pads), diff_pair_nets)
    return build_part_graph(net_pads, net_weight_mult=mult)


#: Netclass fields that mark a class as a differential pair (REQ-SENSE-002).
_PRO_DIFF_KEYS = ("diff_pair_width", "diff_pair_gap", "diff_pair_via_gap")


def _is_pos_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _board_net_names(pcb_path: Path) -> frozenset[str]:
    """Every distinct pad-net name in the board file (for glob expansion)."""
    try:
        content = pcb_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        m.group(2) for m in _NET_PAT.finditer(content) if m.group(2)
    )


def read_diff_pair_nets(pcb_path: Path) -> frozenset[str]:
    """Net names declared as differential pairs in the sibling ``.kicad_pro``.

    Reads the ``net_settings`` written by ``set_board_design_rules`` (§6.4): a
    named (non-``Default``) netclass carrying a positive ``diff_pair_*`` field is
    a differential-pair class, and the nets mapped to it via ``netclass_patterns``
    are its pair nets. Exact patterns are taken verbatim; glob patterns are
    expanded against the board's actual net names. P4 only *reads* this — no new
    declaration surface (REQ-SENSE-002). Returns an empty set when no diff pairs
    are declared or the ``.kicad_pro`` is missing / unreadable.
    """
    pro = pcb_path.with_suffix(".kicad_pro")
    if not pro.exists():
        return frozenset()
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    net_settings = data.get("net_settings")
    if not isinstance(net_settings, dict):
        return frozenset()

    diff_classes: set[str] = set()
    classes = net_settings.get("classes")
    if isinstance(classes, list):
        for cls in classes:
            if not isinstance(cls, dict):
                continue
            name = cls.get("name")
            if not isinstance(name, str) or not name or name == "Default":
                continue
            if any(_is_pos_num(cls.get(k)) for k in _PRO_DIFF_KEYS):
                diff_classes.add(name)
    if not diff_classes:
        return frozenset()

    exact: set[str] = set()
    globs: list[str] = []
    patterns = net_settings.get("netclass_patterns")
    if isinstance(patterns, list):
        for pat in patterns:
            if not isinstance(pat, dict) or pat.get("netclass") not in diff_classes:
                continue
            pattern = pat.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                continue
            if any(ch in pattern for ch in "*?["):
                globs.append(pattern)
            else:
                exact.add(pattern)

    result = set(exact)
    if globs:
        for net in _board_net_names(pcb_path):
            if any(fnmatch.fnmatchcase(net, g) for g in globs):
                result.add(net)
    return frozenset(result)


# ---------------------------------------------------------------------------
# Classification (REQ-CLASS-001..004)
# ---------------------------------------------------------------------------

def classify_parts(
    parts: list[PartRecord],
    edge_connector_refs: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Assign each part a role (first-match-wins, two-pass for decaps)."""
    ic_threshold = get_int("IC_PAD_THRESHOLD")
    connector_prefixes = get_tunable("CONNECTOR_PREFIXES")
    crystal_prefixes = get_tunable("CRYSTAL_PREFIXES")
    assert isinstance(connector_prefixes, tuple)
    assert isinstance(crystal_prefixes, tuple)

    roles: dict[str, str] = {}
    index = build_net_index(parts)

    # Pass 1 — connectors, crystals, ICs (everything that doesn't need IC info).
    for p in sorted(parts, key=lambda q: _ref_key(q["ref"])):
        ref = p["ref"]
        prefix = _ref_prefix(ref)
        lib_up = p["lib_id"].upper()
        if prefix in connector_prefixes or ref in edge_connector_refs:
            roles[ref] = ROLE_CONNECTOR
        elif (
            "CRYSTAL" in lib_up
            or "OSCILLATOR" in lib_up
            or prefix in crystal_prefixes
        ):
            roles[ref] = ROLE_CRYSTAL
        elif p["pad_count"] >= ic_threshold:
            roles[ref] = ROLE_IC

    ic_refs = {r for r, role in roles.items() if role == ROLE_IC}

    def _rail_and_ground(p: PartRecord) -> tuple[str, str] | None:
        """Return (rail_net, ground_net) if the 2-pad cap is power+ground."""
        nets = [
            pad["net_name"] for pad in p["pads"]
            if pad["net_id"] != 0 and pad["net_name"]
        ]
        distinct = sorted(set(nets))
        if len(distinct) != 2:
            return None
        rails = [nn for nn in distinct if classify_net(nn) == "power"
                 and not _is_ground(nn)]
        grounds = [nn for nn in distinct if _is_ground(nn)]
        if len(rails) == 1 and len(grounds) == 1:
            return (rails[0], grounds[0])
        return None

    # Pass 2 — decoupling caps (need ICs resolved first; REQ-DECAP-001/004).
    for p in sorted(parts, key=lambda q: _ref_key(q["ref"])):
        ref = p["ref"]
        if ref in roles:
            continue
        if _ref_prefix(ref) != "C" or p["pad_count"] != 2:
            continue
        rg = _rail_and_ground(p)
        if rg is None:
            continue
        rail, _ground = rg
        rail_refs = {t[0] for t in index.get(rail, [])}
        if rail_refs & ic_refs:
            roles[ref] = ROLE_DECOUPLING_CAP

    # Finalize — passive vs other (REQ-CLASS-005/006).
    for p in parts:
        ref = p["ref"]
        if ref in roles:
            continue
        roles[ref] = (
            ROLE_PASSIVE if _ref_prefix(ref) in _PASSIVE_PREFIXES else ROLE_OTHER
        )
    return roles


# ---------------------------------------------------------------------------
# Decoupling-cap pairing (REQ-DECAP-001..002)
# ---------------------------------------------------------------------------

def pair_decaps(
    parts: list[PartRecord],
    roles: dict[str, str],
) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    """Pair each decoupling cap to an IC, load-balanced + numeric-aware tiebreak."""
    index = build_net_index(parts)
    by_ref = {p["ref"]: p for p in parts}
    ic_refs = {r for r, role in roles.items() if role == ROLE_IC}
    caps = sorted(
        (r for r, role in roles.items() if role == ROLE_DECOUPLING_CAP),
        key=_ref_key,
    )

    assigned: dict[str, int] = {ic: 0 for ic in ic_refs}
    mapping: dict[str, str | None] = {}
    warnings: list[dict[str, str]] = []

    for cap in caps:
        p = by_ref[cap]
        rail = None
        for pad in p["pads"]:
            nn = pad["net_name"]
            if nn and classify_net(nn) == "power" and not _is_ground(nn):
                rail = nn
                break
        candidates = sorted(
            (ic for ic in ic_refs if ic in {t[0] for t in index.get(rail or "", [])}),
            key=lambda ic: (assigned[ic], _ref_key(ic)),
        )
        if not candidates:
            mapping[cap] = None
            warnings.append({
                "type": "decap_unpaired", "cap": cap,
                "reason": "no IC found on the cap's power rail",
            })
            continue
        chosen = candidates[0]
        assigned[chosen] += 1
        mapping[cap] = chosen
    return mapping, warnings


# ---------------------------------------------------------------------------
# Crystal pairing + load caps (P4 — REQ-SENSE-001)
# ---------------------------------------------------------------------------

def pair_crystals(
    parts: list[PartRecord],
    roles: dict[str, str],
    graph: dict[frozenset[str], float],
) -> dict[str, str | None]:
    """Pair each crystal to the IC it shares the most signal weight with.

    Deterministic: crystals iterated numeric-aware; the chosen IC is the highest
    graph weight, tie-broken by lowest reference designator. A crystal sharing no
    signal weight with any IC maps to ``None`` (placed as an ordinary part).
    """
    ic_refs = {r for r, role in roles.items() if role == ROLE_IC}
    crystals = sorted(
        (r for r, role in roles.items() if role == ROLE_CRYSTAL), key=_ref_key,
    )
    mapping: dict[str, str | None] = {}
    for y in crystals:
        candidates = sorted(
            (ic for ic in ic_refs if graph.get(frozenset((y, ic)), 0.0) > 0.0),
            key=lambda ic: (-graph.get(frozenset((y, ic)), 0.0), _ref_key(ic)),
        )
        mapping[y] = candidates[0] if candidates else None
    return mapping


def crystal_load_caps(
    parts: list[PartRecord],
    roles: dict[str, str],
    crystal_pairing: dict[str, str | None],
) -> dict[str, list[str]]:
    """Map each crystal to its load caps — 2-pad ``C`` parts sharing a crystal net.

    These are pulled in with the crystal (REQ-SENSE-001). Decoupling caps are
    excluded (a load cap bridges the oscillator net to ground, not a power rail,
    so it never classifies as a decap).
    """
    by_ref = {p["ref"]: p for p in parts}
    out: dict[str, list[str]] = {}
    for y in crystal_pairing:
        yp = by_ref.get(y)
        if yp is None:
            continue
        y_nets = {
            pad["net_name"] for pad in yp["pads"]
            if pad["net_id"] != 0 and pad["net_name"]
        }
        caps: list[str] = []
        for p in parts:
            ref = p["ref"]
            if ref == y or roles.get(ref) == ROLE_DECOUPLING_CAP:
                continue
            if _ref_prefix(ref) != "C" or p["pad_count"] != 2:
                continue
            p_nets = {
                pad["net_name"] for pad in p["pads"]
                if pad["net_id"] != 0 and pad["net_name"]
            }
            if p_nets & y_nets:
                caps.append(ref)
        out[y] = sorted(caps, key=_ref_key)
    return out


# ---------------------------------------------------------------------------
# Clustering (REQ-CLUSTER-001..003)
# ---------------------------------------------------------------------------

def cluster_parts(
    parts: list[PartRecord],
    roles: dict[str, str],
    decap_pairing: dict[str, str | None],
    anchor_refs: frozenset[str] = frozenset(),
    diff_pair_nets: frozenset[str] = frozenset(),
) -> list[list[str]]:
    """Group non-anchor parts into connectivity clusters (deterministic).

    ``diff_pair_nets`` (P4) boosts declared diff-pair nets in the clustering
    graph, so a pair too weak to bind on its own still clusters its endpoints
    together (REQ-SENSE-002 steers the whole pipeline, not just placement).
    """
    weight_floor = get_float("CLUSTER_WEIGHT_FLOOR")
    graph = _part_graph(parts, diff_pair_nets)

    members = [p for p in parts if p["ref"] not in anchor_refs]
    member_refs = {p["ref"] for p in members}
    by_ref = {p["ref"]: p for p in members}

    label: dict[str, str] = {}

    # Explicit clusters win (REQ-CLUSTER-001).
    explicit = {
        p["ref"] for p in members if p["cluster_key"]
    }
    for ref in explicit:
        label[ref] = "explicit:" + by_ref[ref]["cluster_key"]

    # Graph connected components for the rest (edges >= floor, both ends
    # non-explicit + non-anchor).
    free = sorted(member_refs - explicit, key=_ref_key)
    adj: dict[str, set[str]] = {r: set() for r in free}
    free_set = set(free)
    for pair, w in graph.items():
        if w < weight_floor:
            continue
        a, b = sorted(pair)
        if a in free_set and b in free_set:
            adj[a].add(b)
            adj[b].add(a)

    seen: set[str] = set()
    for start in free:  # already numeric-aware sorted
        if start in seen:
            continue
        comp: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in sorted(adj[cur], key=_ref_key):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comp_label = "graph:" + min(comp, key=_ref_key)
        for ref in comp:
            label[ref] = comp_label

    # Caps travel with their IC (REQ-CLUSTER-003) unless the cap is explicit.
    for cap, ic in decap_pairing.items():
        if cap not in member_refs or ic is None:
            continue
        if cap in explicit:
            continue
        if ic in label:  # IC is a non-anchor member
            label[cap] = label[ic]

    grouped: dict[str, list[str]] = {}
    for ref, lab in label.items():
        grouped.setdefault(lab, []).append(ref)
    clusters = [sorted(refs, key=_ref_key) for refs in grouped.values()]
    clusters.sort(key=lambda c: _ref_key(c[0]))
    return clusters


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _courtyard_of(p: PartRecord) -> tuple[float, float, float, float]:
    return p["courtyard"] if p["courtyard"] is not None else (-2.5, -2.5, 2.5, 2.5)


def _courtyard_size(p: PartRecord) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = _courtyard_of(p)
    return (xmax - xmin, ymax - ymin)


def _courtyard_area(p: PartRecord) -> float:
    w, h = _courtyard_size(p)
    return w * h


def _rotate(dx: float, dy: float, rot_deg: float) -> tuple[float, float]:
    if rot_deg == 0.0:
        return (dx, dy)
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)
    return (dx * c - dy * s, dx * s + dy * c)


def _board_box(
    p: PartRecord, origin: tuple[float, float], rot_deg: float,
) -> tuple[float, float, float, float]:
    """Board-frame courtyard AABB for a part placed at *origin* with *rot_deg*."""
    xmin, ymin, xmax, ymax = _courtyard_of(p)
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    rxs: list[float] = []
    rys: list[float] = []
    for cx, cy in corners:
        rx, ry = _rotate(cx, cy, rot_deg)
        rxs.append(origin[0] + rx)
        rys.append(origin[1] + ry)
    return (min(rxs), min(rys), max(rxs), max(rys))


def _boxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float = 0.0,
) -> bool:
    return not (
        a[2] + gap <= b[0]
        or b[2] + gap <= a[0]
        or a[3] + gap <= b[1]
        or b[3] + gap <= a[1]
    )


def _signal_pads(p: PartRecord) -> list[tuple[str, float, float]]:
    """(net, dx, dy) for the part's signal pads only."""
    out: list[tuple[str, float, float]] = []
    for pad in p["pads"]:
        nn = pad["net_name"]
        if pad["net_id"] != 0 and nn and classify_net(nn) == "signal":
            out.append((nn, pad["dx"], pad["dy"]))
    return out


# ---------------------------------------------------------------------------
# Constructive placement (REQ-PROX-001..005)
# ---------------------------------------------------------------------------

class _Placer:
    """Mutable placement state — keeps net pad clouds for incremental HPWL."""

    def __init__(
        self,
        parts: list[PartRecord],
        anchors: dict[str, tuple[float, float, float]],
        board_rect: tuple[float, float, float, float],
        clearance: float,
    ) -> None:
        self.by_ref = {p["ref"]: p for p in parts}
        self.anchor_refs = frozenset(anchors)
        self.board_rect = board_rect
        self.clearance = clearance
        self.grid = get_float("PROX_CANDIDATE_GRID_MM")
        # ref -> (x, y, rot) of placed parts (anchors first).
        self.plan: dict[str, tuple[float, float, float]] = {}
        # net -> list of board-frame pad coords currently placed.
        self.net_pads: dict[str, list[tuple[float, float]]] = {}
        # scan cursor for parts with no placed neighbour (row-packer order).
        self._cx = board_rect[0]
        self._cy = board_rect[1]
        self._row_h = 0.0
        for ref, pos in anchors.items():
            self._commit(ref, pos)

    # -- pad clouds --------------------------------------------------------
    def _part_pad_coords(
        self, p: PartRecord, origin: tuple[float, float, float],
    ) -> list[tuple[str, float, float]]:
        ox, oy, rot = origin
        out: list[tuple[str, float, float]] = []
        for nn, dx, dy in _signal_pads(p):
            rx, ry = _rotate(dx, dy, rot)
            out.append((nn, ox + rx, oy + ry))
        return out

    def _commit(self, ref: str, origin: tuple[float, float, float]) -> None:
        self.plan[ref] = origin
        for nn, x, y in self._part_pad_coords(self.by_ref[ref], origin):
            self.net_pads.setdefault(nn, []).append((x, y))

    # -- incremental HPWL --------------------------------------------------
    def _delta_hpwl(self, p: PartRecord, origin: tuple[float, float, float]) -> float:
        delta = 0.0
        new_by_net: dict[str, list[tuple[float, float]]] = {}
        for nn, x, y in self._part_pad_coords(p, origin):
            new_by_net.setdefault(nn, []).append((x, y))
        for nn, new_pads in new_by_net.items():
            existing = self.net_pads.get(nn)
            if not existing:
                continue
            ex_xs = [c[0] for c in existing]
            ex_ys = [c[1] for c in existing]
            old_span = (max(ex_xs) - min(ex_xs)) + (max(ex_ys) - min(ex_ys))
            all_xs = ex_xs + [c[0] for c in new_pads]
            all_ys = ex_ys + [c[1] for c in new_pads]
            new_span = (max(all_xs) - min(all_xs)) + (max(all_ys) - min(all_ys))
            delta += new_span - old_span
        return delta

    # -- outline / overlap -------------------------------------------------
    def _clamp_into_outline(
        self, p: PartRecord, origin: tuple[float, float],
    ) -> tuple[float, float] | None:
        bxmin, bymin, bxmax, bymax = self.board_rect
        cxmin, cymin, cxmax, cymax = _courtyard_of(p)
        w, h = cxmax - cxmin, cymax - cymin
        if w > (bxmax - bxmin) or h > (bymax - bymin):
            return None  # courtyard larger than the board area
        ox, oy = origin
        # courtyard left = ox + cxmin must be >= bxmin; right <= bxmax.
        ox = max(bxmin - cxmin, min(ox, bxmax - cxmax))
        oy = max(bymin - cymin, min(oy, bymax - cymax))
        return (round(ox, 4), round(oy, 4))

    def _overlaps_placed(
        self, ref: str, box: tuple[float, float, float, float],
    ) -> bool:
        for other, opos in self.plan.items():
            if other == ref:
                continue
            obox = _board_box(self.by_ref[other], (opos[0], opos[1]), opos[2])
            if _boxes_overlap(box, obox, gap=0.0):
                return True
        return False

    # -- free scan (no placed neighbour) -----------------------------------
    def _next_scan_origin(self, p: PartRecord) -> tuple[float, float]:
        bxmin, bymin, bxmax, bymax = self.board_rect
        w, h = _courtyard_size(p)
        if self._cx + w > bxmax and self._cx > bxmin:
            self._cx = bxmin
            self._cy += self._row_h + self.clearance
            self._row_h = 0.0
        cxmin, cymin, _cxmax, _cymax = _courtyard_of(p)
        origin = (self._cx - cxmin, self._cy - cymin)
        self._cx += w + self.clearance
        self._row_h = max(self._row_h, h)
        return origin

    # -- candidate selection ----------------------------------------------
    def _attractor(self, p: PartRecord) -> tuple[float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for nn, _dx, _dy in _signal_pads(p):
            for x, y in self.net_pads.get(nn, []):
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def place_part(self, ref: str) -> None:
        if ref in self.plan:
            return
        p = self.by_ref[ref]
        attractor = self._attractor(p)
        if attractor is None:
            # No placed neighbour: tile into the next free scan row.
            origin = self._next_scan_origin(p)
            clamped = self._clamp_into_outline(p, origin) or origin
            self._commit(ref, (clamped[0], clamped[1], 0.0))
            return

        # Desired origin places the part's signal-pad centroid on the attractor,
        # then search outward (expanding shells on the candidate grid) for the
        # lowest-HPWL slot that is legal — inside the outline and non-overlapping
        # (REQ-PROX-004). Shells are searched smallest-radius-first so the chosen
        # slot is the closest legal pull-point; once a shell yields any free slot
        # we search one extra shell and stop (bounded + deterministic).
        sp = _signal_pads(p)
        cx = sum(t[1] for t in sp) / len(sp)
        cy = sum(t[2] for t in sp) / len(sp)
        base = (attractor[0] - cx, attractor[1] - cy)

        best_free: tuple[float, float, float, tuple[float, float]] | None = None
        max_r = 40  # 40 * grid mm of near-reach — bounded
        extra_after_free = 1
        free_radius: int | None = None
        for r in range(0, max_r + 1):
            shell = (
                [(0, 0)] if r == 0
                else [
                    (gx, gy)
                    for gy in range(-r, r + 1)
                    for gx in range(-r, r + 1)
                    if max(abs(gx), abs(gy)) == r
                ]
            )
            for gx, gy in sorted(shell):
                cand = (base[0] + gx * self.grid, base[1] + gy * self.grid)
                slot = self._clamp_into_outline(p, cand)
                if slot is None:
                    continue
                box = _board_box(p, slot, 0.0)
                if self._overlaps_placed(ref, box):
                    continue
                delta = round(self._delta_hpwl(p, (slot[0], slot[1], 0.0)), 6)
                key = (delta, slot[1], slot[0], slot)
                if best_free is None or key < best_free:
                    best_free = key
                    if free_radius is None:
                        free_radius = r
            if free_radius is not None and r >= free_radius + extra_after_free:
                break

        if best_free is not None:
            pos = best_free[3]
        else:
            # Crowded near the attractor: take the lowest-HPWL free slot anywhere
            # on the board so construction stays overlap-free (legality before
            # tightness, REQ-PROX-004) instead of stacking parts for the legalizer.
            anywhere = self._find_any_free_slot(ref, p)
            if anywhere is not None:
                pos = anywhere
            else:  # board genuinely full — last-resort scan, legalizer will try
                origin = self._next_scan_origin(p)
                pos = self._clamp_into_outline(p, origin) or origin
        self._commit(ref, (pos[0], pos[1], 0.0))

    def _find_any_free_slot(
        self, ref: str, p: PartRecord,
    ) -> tuple[float, float] | None:
        """Lowest-HPWL non-overlapping slot anywhere on the board (coarse scan)."""
        bxmin, bymin, bxmax, bymax = self.board_rect
        cxmin, cymin, cxmax, cymax = _courtyard_of(p)
        w, h = cxmax - cxmin, cymax - cymin
        if w > (bxmax - bxmin) or h > (bymax - bymin):
            return None
        step = max(self.grid, min(w, h), 1.0)
        best: tuple[float, float, float, tuple[float, float]] | None = None
        y = bymin - cymin
        while y + cymax <= bymax + 1e-9:
            x = bxmin - cxmin
            while x + cxmax <= bxmax + 1e-9:
                ox, oy = round(x, 4), round(y, 4)
                box = _board_box(p, (ox, oy), 0.0)
                if not self._overlaps_placed(ref, box):
                    delta = round(self._delta_hpwl(p, (ox, oy, 0.0)), 6)
                    key = (delta, oy, ox, (ox, oy))
                    if best is None or key < best:
                        best = key
                x += step
            y += step
        return best[3] if best is not None else None

    def _hug_to_ic(
        self, mover_ref: str, host_ref: str, target_mm: float,
        ddx: float, ddy: float,
    ) -> float | None:
        """Place *mover_ref* adjacent to *host_ref*, preferring the (ddx, ddy) side.

        Shared core of decap hugging (REQ-DECAP-003) and crystal hugging
        (REQ-SENSE-001). Searches cardinal sides ordered by alignment to the
        preferred direction and takes the first non-overlapping, in-outline slot;
        returns the achieved centre-to-centre distance (mm), or ``None`` when no
        adjacent slot exists (the caller then scan-places — never-worse). A
        returned distance greater than *target_mm* means "placed near the host but
        not within target"; the caller flags it but keeps the near position rather
        than flinging the part into a scan row (never-worse, REQ-DECAP-005).
        """
        mover = self.by_ref[mover_ref]
        if host_ref not in self.plan:
            return None
        host = self.by_ref[host_ref]
        hox, hoy, hrot = self.plan[host_ref]

        host_box = _board_box(host, (hox, hoy), hrot)
        hcx = (host_box[0] + host_box[2]) / 2.0
        hcy = (host_box[1] + host_box[3]) / 2.0

        mw, mh = _courtyard_size(mover)
        mcxmin, mcymin, mcxmax, mcymax = _courtyard_of(mover)
        m_cx_local = (mcxmin + mcxmax) / 2.0
        m_cy_local = (mcymin + mcymax) / 2.0
        host_half_x = (host_box[2] - host_box[0]) / 2.0
        host_half_y = (host_box[3] - host_box[1]) / 2.0
        step = self.grid

        # Cardinal sides ordered: preferred-direction side first, then by the
        # fixed list index (deterministic tie-break).
        cardinals = [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]
        sides = sorted(
            enumerate(cardinals),
            key=lambda it: (-(it[1][0] * ddx + it[1][1] * ddy), it[0]),
        )

        best: tuple[float, tuple[float, float]] | None = None
        for _idx, side in sides:
            # Hug tighter than the general clearance: shrink the host-to-mover gap
            # (down to courtyards just touching) so the part can land within
            # target_mm when geometrically possible, but never exceed the board
            # clearance when there is slack.
            ax_half = host_half_x if side[0] != 0.0 else host_half_y
            mov_half = mw / 2.0 if side[0] != 0.0 else mh / 2.0
            gap = min(self.clearance, max(0.0, target_mm - ax_half - mov_half))
            for k in range(0, 128):
                base = ax_half + gap + mov_half + k * step
                if side[0] != 0.0:
                    mvx, mvy = hcx + side[0] * base, hcy
                else:
                    mvx, mvy = hcx, hcy + side[1] * base
                origin = (mvx - m_cx_local, mvy - m_cy_local)
                clamped = self._clamp_into_outline(mover, origin)
                if clamped is None:
                    break
                box = _board_box(mover, clamped, 0.0)
                if self._overlaps_placed(mover_ref, box):
                    continue
                amx = clamped[0] + m_cx_local
                amy = clamped[1] + m_cy_local
                dist = round(math.hypot(amx - hcx, amy - hcy), 6)
                # Strictly-closer only: on a distance tie the earlier (preferred)
                # side keeps the slot, honouring the (ddx, ddy) side ordering.
                if best is None or dist < best[0]:
                    best = (dist, clamped)
                break  # first non-overlapping slot on this side
        if best is None:
            return None
        dist, clamped = best
        self._commit(mover_ref, (clamped[0], clamped[1], 0.0))
        return dist

    def place_decap(self, cap_ref: str, ic_ref: str) -> float | None:
        """Place a cap hugging its IC, preferring the power-pad side (REQ-DECAP-003).

        Returns the achieved centre-to-centre distance (mm) or ``None`` when no
        adjacent slot exists (never-worse fallback, REQ-DECAP-005).
        """
        if ic_ref not in self.plan:
            return None
        ic = self.by_ref[ic_ref]
        iox, ioy, irot = self.plan[ic_ref]
        ic_box = _board_box(ic, (iox, ioy), irot)
        icx = (ic_box[0] + ic_box[2]) / 2.0
        icy = (ic_box[1] + ic_box[3]) / 2.0
        rail_xy: tuple[float, float] = (icx, icy)
        for pad in ic["pads"]:
            nn = pad["net_name"]
            if nn and classify_net(nn) == "power" and not _is_ground(nn):
                rx, ry = _rotate(pad["dx"], pad["dy"], irot)
                rail_xy = (iox + rx, ioy + ry)
                break
        ddx, ddy = rail_xy[0] - icx, rail_xy[1] - icy
        return self._hug_to_ic(
            cap_ref, ic_ref, get_float("DECAP_MAX_MM"), ddx, ddy,
        )

    def place_crystal(self, crystal_ref: str, ic_ref: str) -> float | None:
        """Place a crystal adjacent to the IC it clocks (REQ-SENSE-001).

        Prefers the IC side holding the oscillator pins — the centroid of the IC
        pads sharing a net with the crystal. Returns the achieved centre-to-centre
        distance (mm) or ``None`` when no adjacent slot exists (never-worse).
        """
        if ic_ref not in self.plan:
            return None
        crystal = self.by_ref[crystal_ref]
        ic = self.by_ref[ic_ref]
        iox, ioy, irot = self.plan[ic_ref]
        ic_box = _board_box(ic, (iox, ioy), irot)
        icx = (ic_box[0] + ic_box[2]) / 2.0
        icy = (ic_box[1] + ic_box[3]) / 2.0

        xtal_nets = {
            pad["net_name"] for pad in crystal["pads"]
            if pad["net_id"] != 0 and pad["net_name"]
        }
        sxs: list[float] = []
        sys: list[float] = []
        for pad in ic["pads"]:
            if pad["net_id"] != 0 and pad["net_name"] in xtal_nets:
                rx, ry = _rotate(pad["dx"], pad["dy"], irot)
                sxs.append(iox + rx)
                sys.append(ioy + ry)
        if sxs:
            ddx = sum(sxs) / len(sxs) - icx
            ddy = sum(sys) / len(sys) - icy
        else:
            ddx, ddy = 0.0, 0.0
        return self._hug_to_ic(
            crystal_ref, ic_ref, get_float("SENSE_MAX_MM"), ddx, ddy,
        )


# ---------------------------------------------------------------------------
# Cluster ordering (P2 area-descending; P3 signal-flow, REQ-FLOW-001..003)
# ---------------------------------------------------------------------------

def _order_clusters_by_area(
    clusters: list[list[str]], by_ref: dict[str, PartRecord],
) -> list[list[str]]:
    """P2 order: total courtyard area descending, tie-broken by min ref."""
    def cluster_area(c: list[str]) -> float:
        return sum(_courtyard_area(by_ref[r]) for r in c if r in by_ref)

    return sorted(
        clusters,
        key=lambda c: (-cluster_area(c), _ref_key(min(c, key=_ref_key))),
    )


def _anchor_centre(
    p: PartRecord, pos: tuple[float, float, float],
) -> tuple[float, float]:
    """Board-frame courtyard centre of an anchored part at *pos*."""
    box = _board_box(p, (pos[0], pos[1]), pos[2])
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _connector_flow_kind(p: PartRecord) -> str:
    """Label a connector ``"input"`` / ``"output"`` / ``""`` from its pad nets.

    Input-ish tokens win over output-ish only when they are the *sole* match, so
    a connector carrying both an input and an output token stays ambiguous
    (``""``) and is resolved by the physical-edge fallback instead.
    """
    in_tokens = get_tunable("FLOW_INPUT_NET_TOKENS")
    out_tokens = get_tunable("FLOW_OUTPUT_NET_TOKENS")
    assert isinstance(in_tokens, tuple)
    assert isinstance(out_tokens, tuple)
    names = [pad["net_name"].upper() for pad in p["pads"] if pad["net_name"]]
    has_in = any(tok.upper() in nn for nn in names for tok in in_tokens)
    has_out = any(tok.upper() in nn for nn in names for tok in out_tokens)
    if has_in and not has_out:
        return "input"
    if has_out and not has_in:
        return "output"
    return ""


def order_clusters_by_flow(
    clusters: list[list[str]],
    parts: list[PartRecord],
    roles: dict[str, str],
    anchors: dict[str, tuple[float, float, float]],
    graph: dict[frozenset[str], float],
    board_rect: tuple[float, float, float, float],
) -> list[list[str]] | None:
    """Order clusters input→output along the flow axis (REQ-FLOW-001).

    Returns ``None`` when there are fewer than two distinguishable endpoints, so
    the caller keeps the exact P2 area order (graceful no-op, REQ-FLOW-002).
    """
    by_ref = {p["ref"]: p for p in parts}
    conns = sorted(
        (
            r for r in anchors
            if roles.get(r) == ROLE_CONNECTOR and r in by_ref
        ),
        key=_ref_key,
    )
    if len(conns) < 2:
        return None

    centre = {c: _anchor_centre(by_ref[c], anchors[c]) for c in conns}
    kinds = {c: _connector_flow_kind(by_ref[c]) for c in conns}
    inputs = [c for c in conns if kinds[c] == "input"]
    outputs = [c for c in conns if kinds[c] == "output"]

    in_ep: str | None = None
    out_ep: str | None = None
    if len(inputs) == 1 and len(outputs) == 1 and inputs[0] != outputs[0]:
        in_ep, out_ep = inputs[0], outputs[0]
    else:
        # Physical-edge fallback: the two connectors furthest apart. Input is the
        # one at the lower coordinate on the flow axis (decided just below).
        best_pair: tuple[float, str, str] | None = None
        for i in range(len(conns)):
            for j in range(i + 1, len(conns)):
                a, b = conns[i], conns[j]
                d = math.hypot(
                    centre[a][0] - centre[b][0], centre[a][1] - centre[b][1],
                )
                cand = (-d, a, b)
                if best_pair is None or cand < best_pair:
                    best_pair = cand
        if best_pair is None:
            return None
        _neg_d, a, b = best_pair
        in_ep, out_ep = a, b  # orientation fixed after the axis is known

    ic, oc = centre[in_ep], centre[out_ep]
    bxmin, bymin, bxmax, bymax = board_rect
    bw, bh = bxmax - bxmin, bymax - bymin
    dx, dy = abs(ic[0] - oc[0]), abs(ic[1] - oc[1])
    if dx > dy:
        axis = 0
    elif dy > dx:
        axis = 1
    else:
        axis = 0 if bw > bh else (1 if bh > bw else (
            0 if get_str("FLOW_AXIS_TIE_BREAK").lower() == "x" else 1
        ))

    # Fallback endpoints: on the chosen axis, input is the lower coordinate.
    if not (len(inputs) == 1 and len(outputs) == 1 and inputs[0] != outputs[0]):
        if centre[in_ep][axis] > centre[out_ep][axis]:
            in_ep, out_ep = out_ep, in_ep

    def weight_to(cluster: list[str], endpoint: str) -> float:
        return sum(
            graph.get(frozenset((m, endpoint)), 0.0)
            for m in cluster if m != endpoint
        )

    def cluster_area(c: list[str]) -> float:
        return sum(_courtyard_area(by_ref[r]) for r in c if r in by_ref)

    # Input-biased clusters first: descending (w_in - w_out); ties keep the P2
    # area order so equal-flow clusters do not reshuffle (determinism).
    ordered = sorted(
        clusters,
        key=lambda c: (
            -(weight_to(c, in_ep) - weight_to(c, out_ep)),
            -cluster_area(c),
            _ref_key(min(c, key=_ref_key)),
        ),
    )
    return ordered


def place(
    parts: list[PartRecord],
    roles: dict[str, str],
    clusters: list[list[str]],
    decap_pairing: dict[str, str | None],
    anchors: dict[str, tuple[float, float, float]],
    board_rect: tuple[float, float, float, float],
    clearance: float,
    cluster_order: list[list[str]] | None = None,
    diff_pair_nets: frozenset[str] = frozenset(),
) -> tuple[dict[str, tuple[float, float, float]], list[dict[str, str]]]:
    """Constructive net-aware placement + bounded local improvement.

    Returns ``(plan, warnings)``. ``plan`` maps every non-anchor ref to
    ``(x, y, 0.0)``; anchors are not in the plan (they never move). When
    ``cluster_order`` is given it fixes the cluster placement sequence (P3 flow
    ordering); otherwise the P2 area-descending order is used (REQ-BACK).
    ``diff_pair_nets`` (P4) steers the graph weighting toward declared diff pairs.
    """
    placer = _Placer(parts, anchors, board_rect, clearance)
    warnings: list[dict[str, str]] = []
    by_ref = placer.by_ref
    graph = _part_graph(parts, diff_pair_nets)
    decap_max = get_float("DECAP_MAX_MM")
    sense_max = get_float("SENSE_MAX_MM")

    ordered = (
        cluster_order if cluster_order is not None
        else _order_clusters_by_area(clusters, by_ref)
    )

    # --- Phase A: constructive placement, IC then its decaps. -------------
    # A decoupling cap carries only power/ground nets (no signal pad → no
    # attractor), so it cannot find its own slot; it is hugged to its IC right
    # after the IC lands (REQ-DECAP-003), reserving the adjacent slot before
    # lower-degree cluster members fill in around them (spec §6.4).
    all_caps = {r for r, role in roles.items() if role == ROLE_DECOUPLING_CAP}
    caps_of_ic: dict[str, list[str]] = {}
    for cap, ic in decap_pairing.items():
        if ic is not None and roles.get(cap) == ROLE_DECOUPLING_CAP:
            caps_of_ic.setdefault(ic, []).append(cap)
    for ic in caps_of_ic:
        caps_of_ic[ic].sort(key=_ref_key)

    # Sensitive placement (P4 — REQ-SENSE-001): a crystal hugs the IC it clocks
    # right after the IC lands, and its load caps ride the crystal — mirroring
    # decap hugging, with the same never-worse fallback.
    crystal_pairing = pair_crystals(parts, roles, graph)
    cryst_caps = crystal_load_caps(parts, roles, crystal_pairing)
    crystals_of_ic: dict[str, list[str]] = {}
    for y, ic in crystal_pairing.items():
        if ic is not None:
            crystals_of_ic.setdefault(ic, []).append(y)
    for ic in crystals_of_ic:
        crystals_of_ic[ic].sort(key=_ref_key)
    all_crystals = set(crystal_pairing)
    crystal_cap_set = {c for caps in cryst_caps.values() for c in caps}
    special = all_caps | all_crystals | crystal_cap_set

    def _hug_decaps(ic_ref: str, present_caps: set[str]) -> None:
        for cap in caps_of_ic.get(ic_ref, []):
            if cap not in present_caps or cap in anchors or cap in placer.plan:
                continue
            dist = placer.place_decap(cap, ic_ref)
            if dist is None:
                placer.place_part(cap)  # no adjacent slot — scan fallback
                warnings.append({
                    "type": "decap_fallback", "cap": cap, "ic": ic_ref,
                    "reason": "no adjacent slot near the IC inside the outline",
                })
            elif dist > decap_max:
                warnings.append({
                    "type": "decap_fallback", "cap": cap, "ic": ic_ref,
                    "reason": "nearest non-overlapping slot exceeds DECAP_MAX_MM",
                })

    def _hug_load_caps(crystal_ref: str, present_caps: set[str]) -> None:
        for cap in cryst_caps.get(crystal_ref, []):
            if cap not in present_caps or cap in anchors or cap in placer.plan:
                continue
            # Load caps ride the crystal (crystal is the host); scan fallback if
            # no adjacent slot — an attractor on the shared XTAL net still pulls
            # them close (never-worse).
            if placer._hug_to_ic(cap, crystal_ref, sense_max, 0.0, 0.0) is None:
                placer.place_part(cap)

    def _hug_crystals(
        ic_ref: str, present_crystals: set[str], present_caps: set[str],
    ) -> None:
        for y in crystals_of_ic.get(ic_ref, []):
            if y not in present_crystals or y in anchors or y in placer.plan:
                continue
            dist = placer.place_crystal(y, ic_ref)
            if dist is None:
                placer.place_part(y)  # no adjacent slot — scan fallback
                warnings.append({
                    "type": "sense_fallback", "part": y, "ic": ic_ref,
                    "reason": "no adjacent slot near the IC inside the outline",
                })
            elif dist > sense_max:
                warnings.append({
                    "type": "sense_fallback", "part": y, "ic": ic_ref,
                    "reason": "nearest non-overlapping slot exceeds SENSE_MAX_MM",
                })
            _hug_load_caps(y, present_caps)

    for cluster in ordered:
        present = [r for r in cluster if r in by_ref and r not in anchors]
        present_caps = {r for r in present if r in all_caps}
        present_crystals = {r for r in present if r in all_crystals}
        present_load_caps = {r for r in present if r in crystal_cap_set}
        non_caps = [r for r in present if r not in special]
        member_set = set(present)

        def degree(r: str, members: set[str]) -> float:
            return sum(
                w for pair, w in graph.items()
                if r in pair and (pair - {r}) <= members
            )

        non_caps.sort(key=lambda r: (-degree(r, member_set), _ref_key(r)))
        for ref in non_caps:
            placer.place_part(ref)
            if roles.get(ref) == ROLE_IC:
                _hug_decaps(ref, present_caps)
                _hug_crystals(ref, present_crystals, present_load_caps)

    # Safety net: any ordinary (non-special) part not in a cluster (defensive).
    for p in sorted(parts, key=lambda q: _ref_key(q["ref"])):
        ref = p["ref"]
        if ref in anchors or ref in placer.plan or ref in special:
            continue
        placer.place_part(ref)

    # Crystals whose IC was an anchor or in another cluster, plus any left over.
    for y in sorted(all_crystals, key=_ref_key):
        if y in anchors or y in placer.plan:
            continue
        ic = crystal_pairing.get(y)
        dist = (
            placer.place_crystal(y, ic)
            if ic is not None and ic in placer.plan else None
        )
        if dist is None:
            placer.place_part(y)
            if ic is not None:
                warnings.append({
                    "type": "sense_fallback", "part": y, "ic": ic,
                    "reason": "paired IC unavailable for adjacent placement",
                })
        elif dist > sense_max:
            warnings.append({
                "type": "sense_fallback", "part": y, "ic": ic or "",
                "reason": "nearest non-overlapping slot exceeds SENSE_MAX_MM",
            })
        _hug_load_caps(y, set(cryst_caps.get(y, [])))

    # Any load caps whose crystal never landed adjacent — ordinary placement.
    for cap in sorted(crystal_cap_set, key=_ref_key):
        if cap in anchors or cap in placer.plan:
            continue
        placer.place_part(cap)

    # Caps whose IC was an anchor or in another cluster, plus any left over.
    for cap in sorted(all_caps, key=_ref_key):
        if cap in anchors or cap in placer.plan:
            continue
        ic = decap_pairing.get(cap)
        dist = placer.place_decap(cap, ic) if ic is not None and ic in placer.plan else None
        if dist is None:
            placer.place_part(cap)
            if ic is not None:
                warnings.append({
                    "type": "decap_fallback", "cap": cap, "ic": ic,
                    "reason": "paired IC unavailable for adjacent placement",
                })
        elif dist > decap_max:
            warnings.append({
                "type": "decap_fallback", "cap": cap, "ic": ic or "",
                "reason": "nearest non-overlapping slot exceeds DECAP_MAX_MM",
            })

    # --- Phase B: bounded local improvement (moves an IC with its group). ---
    # The rigid move group keeps a crystal + its load caps hugging their IC (and
    # decaps hugging theirs) through improvement (REQ-SENSE-001 never-worse).
    group_of_ic: dict[str, list[str]] = {
        ic: list(caps) for ic, caps in caps_of_ic.items()
    }
    for ic, ys in crystals_of_ic.items():
        for y in ys:
            group_of_ic.setdefault(ic, []).append(y)
            group_of_ic[ic].extend(cryst_caps.get(y, []))
    improved = _improve(
        parts,
        {r: pos for r, pos in placer.plan.items() if r not in anchors},
        anchors,
        board_rect,
        group_of_ic,
    )
    for ref, pos in improved.items():
        placer.plan[ref] = pos

    # Outline-containment warnings (REQ-PROX-004).
    bxmin, bymin, bxmax, bymax = board_rect
    for ref, pos in placer.plan.items():
        if ref in anchors:
            continue
        box = _board_box(by_ref[ref], (pos[0], pos[1]), pos[2])
        if box[0] < bxmin - 1e-6 or box[1] < bymin - 1e-6 or \
           box[2] > bxmax + 1e-6 or box[3] > bymax + 1e-6:
            warnings.append({
                "type": "board_area_full", "ref": ref,
                "reason": "courtyard could not fit inside the board outline",
            })

    plan = {r: pos for r, pos in placer.plan.items() if r not in anchors}
    return plan, warnings


def _total_hpwl_of(
    parts: list[PartRecord],
    plan: dict[str, tuple[float, float, float]],
    anchors: dict[str, tuple[float, float, float]],
) -> float:
    by_ref = {p["ref"]: p for p in parts}
    pos_of = dict(plan)
    pos_of.update(anchors)
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for ref, pos in pos_of.items():
        p = by_ref.get(ref)
        if p is None:
            continue
        ox, oy, rot = pos
        for nn, dx, dy in _signal_pads(p):
            rx, ry = _rotate(dx, dy, rot)
            net_pads.setdefault(nn, []).append((ox + rx, oy + ry))
    total = 0.0
    for nn, pads in net_pads.items():
        if len(pads) < 2:
            continue
        xs = [c[0] for c in pads]
        ys = [c[1] for c in pads]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _improve(
    parts: list[PartRecord],
    plan: dict[str, tuple[float, float, float]],
    anchors: dict[str, tuple[float, float, float]],
    board_rect: tuple[float, float, float, float],
    caps_of_ic: dict[str, list[str]] | None = None,
) -> dict[str, tuple[float, float, float]]:
    """Bounded local improvement (REQ-PROX-003): strict-decrease, legality kept.

    An IC moves together with its decoupling caps as a rigid group, so the caps
    stay hugging it (decaps have no signal pads, so they never initiate a move
    themselves).
    """
    passes = get_int("PROX_IMPROVE_PASSES")
    grid = get_float("PROX_CANDIDATE_GRID_MM")
    caps_of_ic = caps_of_ic or {}
    cap_to_ic = {cap: ic for ic, caps in caps_of_ic.items() for cap in caps}
    by_ref = {p["ref"]: p for p in parts}
    plan = dict(plan)
    offsets = [
        (gx * grid, gy * grid)
        for gy in (-1, 0, 1) for gx in (-1, 0, 1)
        if not (gx == 0 and gy == 0)
    ]
    bxmin, bymin, bxmax, bymax = board_rect

    def group_of(ref: str) -> list[str]:
        grp = [ref]
        grp.extend(c for c in caps_of_ic.get(ref, []) if c in plan)
        return grp

    def legal_group(
        group: list[str], delta: tuple[float, float],
        pos_all: dict[str, tuple[float, float, float]],
    ) -> bool:
        gset = set(group)
        for ref in group:
            cur = plan[ref]
            nb = _board_box(by_ref[ref], (cur[0] + delta[0], cur[1] + delta[1]), 0.0)
            if nb[0] < bxmin - 1e-6 or nb[1] < bymin - 1e-6 or \
               nb[2] > bxmax + 1e-6 or nb[3] > bymax + 1e-6:
                return False
            for other, opos in pos_all.items():
                if other in gset:
                    continue
                obox = _board_box(by_ref[other], (opos[0], opos[1]), opos[2])
                if _boxes_overlap(nb, obox, gap=0.0):
                    return False
        return True

    for _ in range(passes):
        moved = False
        for ref in sorted(plan, key=_ref_key):
            if ref in cap_to_ic:
                continue  # caps move only with their IC
            cur = plan[ref]
            best_h = _total_hpwl_of(parts, plan, anchors)
            best_delta: tuple[float, float] | None = None
            pos_all = dict(plan)
            pos_all.update(anchors)
            group = group_of(ref)
            for ox, oy in offsets:
                if not legal_group(group, (ox, oy), pos_all):
                    continue
                trial = dict(plan)
                trial[ref] = (round(cur[0] + ox, 4), round(cur[1] + oy, 4), 0.0)
                h = _total_hpwl_of(parts, trial, anchors)
                if h < best_h - 1e-9:
                    best_h = h
                    best_delta = (ox, oy)
            if best_delta is not None:
                for member in group:
                    mp = plan[member]
                    plan[member] = (
                        round(mp[0] + best_delta[0], 4),
                        round(mp[1] + best_delta[1], 4),
                        0.0,
                    )
                moved = True
        if not moved:
            break
    return plan


# ---------------------------------------------------------------------------
# Legalization (REQ-LEGAL-001..003)
# ---------------------------------------------------------------------------

def legalize(
    parts: list[PartRecord],
    plan: dict[str, tuple[float, float, float]],
    anchors: dict[str, tuple[float, float, float]],
    board_rect: tuple[float, float, float, float],
    clearance: float,
) -> tuple[dict[str, tuple[float, float, float]], list[dict[str, object]]]:
    """Reseat overlapping courtyards to free space; bounded, always terminates.

    Rather than push pairs apart by penetration depth (which oscillates and can
    shove a part into a third — making things worse), this reseats each
    overlapping part at the **nearest free slot** to its current position. Smaller
    courtyards move first (REQ-LEGAL-003 — disturb the looser part); anchors never
    move (only their overlapping neighbours are reseated). At most
    LEGALIZE_MAX_PASSES passes, then any residual overlap is reported as a
    structured warning rather than looping (REQ-LEGAL-002).
    """
    max_passes = get_int("LEGALIZE_MAX_PASSES")
    grid = get_float("PROX_CANDIDATE_GRID_MM")
    by_ref = {p["ref"]: p for p in parts}
    plan = dict(plan)
    pos_of = dict(plan)
    pos_of.update(anchors)
    bxmin, bymin, bxmax, bymax = board_rect
    warnings: list[dict[str, object]] = []

    def box_of(ref: str) -> tuple[float, float, float, float]:
        pos = pos_of[ref]
        return _board_box(by_ref[ref], (pos[0], pos[1]), pos[2])

    def overlaps_any(ref: str) -> bool:
        rb = box_of(ref)
        for other in pos_of:
            if other == ref:
                continue
            if _boxes_overlap(rb, box_of(other), gap=0.0):
                return True
        return False

    def free_at(ref: str, origin: tuple[float, float]) -> bool:
        cand = _board_box(by_ref[ref], origin, 0.0)
        if cand[0] < bxmin - 1e-6 or cand[1] < bymin - 1e-6 or \
           cand[2] > bxmax + 1e-6 or cand[3] > bymax + 1e-6:
            return False
        for other in pos_of:
            if other == ref:
                continue
            if _boxes_overlap(cand, box_of(other), gap=0.0):
                return False
        return True

    def nearest_free(ref: str) -> tuple[float, float] | None:
        cur = pos_of[ref]
        # Coarse outward shells from the current position keep displacement small.
        cxmin, cymin, cxmax, cymax = _courtyard_of(by_ref[ref])
        step = max(grid, min(cxmax - cxmin, cymax - cymin), 1.0)
        for r in range(1, 200):
            shell = [
                (gx, gy)
                for gy in range(-r, r + 1)
                for gx in range(-r, r + 1)
                if max(abs(gx), abs(gy)) == r
            ]
            for gx, gy in sorted(shell):
                origin = (round(cur[0] + gx * step, 4), round(cur[1] + gy * step, 4))
                if free_at(ref, origin):
                    return origin
            # Stop once shells exceed the board extent (nothing further to find).
            if r * step > (bxmax - bxmin) + (bymax - bymin):
                break
        return None

    # Only non-anchor parts are movable.
    movable = [r for r in plan if r not in anchors]
    for _ in range(max_passes):
        bad = sorted(
            (r for r in movable if overlaps_any(r)),
            key=lambda r: (_courtyard_area(by_ref[r]), _ref_key(r)),
        )
        if not bad:
            break
        moved = False
        for ref in bad:
            if not overlaps_any(ref):
                continue  # an earlier reseat in this pass already cleared it
            slot = nearest_free(ref)
            if slot is not None:
                pos_of[ref] = (slot[0], slot[1], pos_of[ref][2])
                plan[ref] = pos_of[ref]
                moved = True
        if not moved:
            break

    # Report any residual overlaps instead of looping (REQ-LEGAL-002).
    residual: list[list[str]] = []
    refs = sorted(pos_of, key=_ref_key)
    for ai in range(len(refs)):
        for bi in range(ai + 1, len(refs)):
            a, b = refs[ai], refs[bi]
            if a in anchors and b in anchors:
                continue
            if _boxes_overlap(box_of(a), box_of(b), gap=0.0):
                residual.append([a, b])
    if residual:
        warnings.append({"type": "legalize_incomplete", "unresolved": residual})
    return plan, warnings


# ---------------------------------------------------------------------------
# Orientation normalization (P3 — REQ-ORIENT-001..004)
# ---------------------------------------------------------------------------

def _quantize_rot(rot: float, quantum: float) -> float:
    """Rotation snapped to the nearest quantum, folded into ``[0, 360)``."""
    return round((rot % 360.0) / quantum) * quantum % 360.0


def _orientation_eligible(ref: str, roles: dict[str, str]) -> bool:
    """Passives / unclassified parts may be rotated; ICs/connectors/crystals/
    decaps keep their placed orientation (REQ-ORIENT-002)."""
    return roles.get(ref) in (ROLE_PASSIVE, ROLE_OTHER)


def normalize_orientations(
    parts: list[PartRecord],
    plan: dict[str, tuple[float, float, float]],
    roles: dict[str, str],
    anchors: dict[str, tuple[float, float, float]],
    board_rect: tuple[float, float, float, float],
) -> tuple[dict[str, tuple[float, float, float]], list[dict[str, object]]]:
    """Rotate-for-HPWL then normalize like footprints to one orientation.

    (a) For each eligible part, accept a quantized rotation only on a **strict**
    Total-HPWL decrease that stays legal — this may leave a footprint family at
    mixed orientations. (b) Snap off-modal family members to the family's modal
    rotation when it does **not increase** HPWL and stays legal, lifting
    ``orientation_consistency`` without regressing HPWL (REQ-ORIENT-004). Both
    passes are bounded, deterministic, and never-worse; anchors/ICs/connectors/
    crystals are never rotated (REQ-ORIENT-002).
    """
    by_ref = {p["ref"]: p for p in parts}
    plan = dict(plan)
    bxmin, bymin, bxmax, bymax = board_rect
    quantum = get_float("ORIENT_ROTATION_QUANTUM_DEG")
    n_cand = max(1, int(round(360.0 / quantum)))
    candidates = [round((i * quantum) % 360.0, 4) for i in range(n_cand)]
    warnings: list[dict[str, object]] = []

    def legal_single(ref: str, x: float, y: float, rot: float) -> bool:
        nb = _board_box(by_ref[ref], (x, y), rot)
        if nb[0] < bxmin - 1e-6 or nb[1] < bymin - 1e-6 or \
           nb[2] > bxmax + 1e-6 or nb[3] > bymax + 1e-6:
            return False
        pos_all = dict(plan)
        pos_all.update(anchors)
        for other, opos in pos_all.items():
            if other == ref:
                continue
            obox = _board_box(by_ref[other], (opos[0], opos[1]), opos[2])
            if _boxes_overlap(nb, obox, gap=0.0):
                return False
        return True

    eligible = sorted(
        (r for r in plan if r in by_ref and _orientation_eligible(r, roles)),
        key=_ref_key,
    )

    # (a) Rotation-for-HPWL: strict-decrease, legality-preserving, greedy.
    for ref in eligible:
        x, y, cur = plan[ref]
        best_h = _total_hpwl_of(parts, plan, anchors)
        best_rot: float | None = None
        for rot in candidates:
            if rot == cur or not legal_single(ref, x, y, rot):
                continue
            trial = dict(plan)
            trial[ref] = (x, y, rot)
            h = _total_hpwl_of(parts, trial, anchors)
            if h < best_h - 1e-9:
                best_h = h
                best_rot = rot
        if best_rot is not None:
            plan[ref] = (x, y, best_rot)

    # (b) Family normalization: snap off-modal members to the modal rotation
    # when HPWL does not increase (families iterated by sorted lib id).
    families: dict[str, list[str]] = {}
    for ref in eligible:
        families.setdefault(by_ref[ref]["lib_id"], []).append(ref)

    for lib_id in sorted(families):
        members = sorted(families[lib_id], key=_ref_key)
        if len(members) < 2:
            continue
        buckets: dict[float, int] = {}
        for ref in members:
            q = _quantize_rot(plan[ref][2], quantum)
            buckets[q] = buckets.get(q, 0) + 1
        # Modal rotation; tie broken toward the smallest rotation value.
        modal = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for ref in members:
            x, y, cur = plan[ref]
            if _quantize_rot(cur, quantum) == modal:
                continue
            if not legal_single(ref, x, y, modal):
                continue
            base_h = _total_hpwl_of(parts, plan, anchors)
            trial = dict(plan)
            trial[ref] = (x, y, modal)
            h = _total_hpwl_of(parts, trial, anchors)
            if h <= base_h + 1e-9:
                plan[ref] = (x, y, modal)

    return plan, warnings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class PlacementPlan(TypedDict):
    plan: dict[str, tuple[float, float, float]]
    roles: dict[str, str]
    clusters: list[list[str]]
    decap_pairing: dict[str, str | None]
    warnings: list[dict[str, object]]


def plan_placement(
    parts: list[PartRecord],
    board_rect: tuple[float, float, float, float],
    clearance: float,
    anchors: dict[str, tuple[float, float, float]] | None = None,
    edge_connector_refs: frozenset[str] = frozenset(),
    diff_pair_nets: frozenset[str] = frozenset(),
) -> PlacementPlan:
    """Full net-aware plan: classify → pair → cluster → order-by-flow → place →
    legalize → normalize-orientations (P3 pipeline). ``diff_pair_nets`` (P4)
    boosts the graph weight of declared differential pairs (REQ-SENSE-002)."""
    anchors = dict(anchors or {})
    roles = classify_parts(parts, edge_connector_refs)
    decap_pairing, decap_warnings = pair_decaps(parts, roles)
    clusters = cluster_parts(
        parts, roles, decap_pairing, frozenset(anchors), diff_pair_nets,
    )

    def _run(order: list[list[str]] | None) -> tuple[
        dict[str, tuple[float, float, float]], list[dict[str, str]],
        list[dict[str, object]],
    ]:
        pl, pw = place(
            parts, roles, clusters, decap_pairing, anchors, board_rect,
            clearance, cluster_order=order, diff_pair_nets=diff_pair_nets,
        )
        pl, lw = legalize(parts, pl, anchors, board_rect, clearance)
        return pl, pw, lw

    # Flow ordering (P3 §4). None → graceful no-op (P2 area order). When flow
    # order is available, keep it only if its Total HPWL is no worse than the
    # area order — a strict never-worse guard vs P2 (spec §5).
    graph = _part_graph(parts, diff_pair_nets)
    flow_order = order_clusters_by_flow(
        clusters, parts, roles, anchors, graph, board_rect,
    )
    plan, place_warnings, legal_warnings = _run(None)
    if flow_order is not None:
        f_plan, f_pw, f_lw = _run(flow_order)
        if _total_hpwl_of(parts, f_plan, anchors) <= _total_hpwl_of(
            parts, plan, anchors,
        ):
            plan, place_warnings, legal_warnings = f_plan, f_pw, f_lw

    # Orientation normalization (P3 §3) — refine the legal layout, never-worse.
    plan, orient_warnings = normalize_orientations(
        parts, plan, roles, anchors, board_rect,
    )

    warnings: list[dict[str, object]] = []
    warnings.extend({k: v for k, v in w.items()} for w in decap_warnings)
    warnings.extend(place_warnings)  # type: ignore[arg-type]
    warnings.extend(legal_warnings)
    warnings.extend(orient_warnings)
    return PlacementPlan(
        plan=plan,
        roles=roles,
        clusters=clusters,
        decap_pairing=decap_pairing,
        warnings=warnings,
    )


def compute_net_aware_plan(
    parts: list[PartRecord],
    board_x: float,
    board_y: float,
    board_width: float,
    board_height: float,
    clearance: float,
    anchors: list[str] | None = None,
    diff_pair_nets: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, float, float, float]], list[dict[str, object]], float]:
    """Plan a net-aware layout for a board area; backend-agnostic apply contract.

    Returns ``(items, warnings, total_area_mm2)`` where ``items`` is the sorted
    list of ``(ref, x, y, rot)`` to apply (non-anchor refs only), so any backend
    can apply it through its own ``move_component``. The usable region is inset by
    ``clearance`` on every side (row-packer parity). ``diff_pair_nets`` comes from
    :func:`read_diff_pair_nets` on the caller's board path (REQ-SENSE-002).
    """
    by_ref = {p["ref"]: p for p in parts}
    anchor_set = set(anchors or [])
    anchor_pos = {r: by_ref[r]["pos"] for r in anchor_set if r in by_ref}
    board_rect = (
        board_x + clearance,
        board_y + clearance,
        board_x + board_width - clearance,
        board_y + board_height - clearance,
    )
    res = plan_placement(
        parts, board_rect, clearance, anchors=anchor_pos,
        diff_pair_nets=diff_pair_nets,
    )
    plan = res["plan"]
    items = [
        (ref, round(plan[ref][0], 4), round(plan[ref][1], 4), plan[ref][2])
        for ref in sorted(plan, key=_ref_key)
    ]
    total_area = sum(_courtyard_area(by_ref[r]) for r in plan if r in by_ref)
    return items, list(res["warnings"]), round(total_area, 2)


def measure_decap_distances(
    parts: list[PartRecord],
    roles: dict[str, str],
    decap_pairing: dict[str, str | None],
    positions: dict[str, tuple[float, float, float]],
) -> tuple[float | None, float | None]:
    """Centre-to-centre cap→IC distances (mm): (max, mean) over paired caps.

    ``positions`` maps ref -> (x, y, rot_deg) board-frame origin. Returns
    ``(None, None)`` when there are no paired decoupling caps.
    """
    by_ref = {p["ref"]: p for p in parts}

    def centre(ref: str) -> tuple[float, float] | None:
        p = by_ref.get(ref)
        pos = positions.get(ref)
        if p is None or pos is None:
            return None
        xmin, ymin, xmax, ymax = _courtyard_of(p)
        clx, cly = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        rx, ry = _rotate(clx, cly, pos[2])
        return (pos[0] + rx, pos[1] + ry)

    dists: list[float] = []
    for cap, ic in sorted(decap_pairing.items(), key=lambda kv: _ref_key(kv[0])):
        if ic is None or roles.get(cap) != ROLE_DECOUPLING_CAP:
            continue
        cc = centre(cap)
        ic_c = centre(ic)
        if cc is None or ic_c is None:
            continue
        dists.append(math.hypot(cc[0] - ic_c[0], cc[1] - ic_c[1]))
    if not dists:
        return (None, None)
    return (round(max(dists), 4), round(sum(dists) / len(dists), 4))
