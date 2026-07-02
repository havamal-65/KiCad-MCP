"""Named constants + net classification for net-aware placement.

This module is pure configuration and small pure helpers. It is imported by
``placement_metrics`` in P1 and by the placement engine (P2-P4) later. It has no
KiCad / bridge / file-system dependency so it can be imported from anywhere
without a cycle.

All distances are millimetres (float). No inline magic numbers live outside this
module (project rule, REQ-CFG-001).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import SupportsFloat, SupportsInt

# ---------------------------------------------------------------------------
# Net classification (REQ-GRAPH-003)
# ---------------------------------------------------------------------------

#: Exact (case-insensitive) net names treated as power/ground. These contribute
#: zero proximity weight to the part graph and are excluded from Total HPWL;
#: their connectivity is handled by decoupling-cap pairing in P2, not proximity.
#:
#: VIN / VOUT are intentionally **excluded** from this set (requirements Q2):
#: they are real rails but often the very nets whose length matters at a
#: regulator, so including them would distort HPWL toward zero on power-converter
#: boards. They are therefore treated as signal by default. Override via
#: ``load_overrides`` if a board wants them excluded from proximity.
POWER_NET_EXACT: frozenset[str] = frozenset({
    "GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "GNDPWR",
    "VSS", "VSSA", "VCC", "VDD", "VDDA", "VEE", "VBUS", "VBAT",
})

#: Voltage-rail name pattern: +3V3, +5V, -12V, 1V8, 3V3, etc.
POWER_NET_PATTERN: re.Pattern[str] = re.compile(r"^[+-]?\d+V\d*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Graph / metric tunables (REQ-GRAPH-005, REQ-METRIC-004)
# ---------------------------------------------------------------------------

#: A signal net touching more than this many distinct footprints is treated as a
#: quasi-bus and contributes **zero** proximity weight to the part graph, so a
#: stray wide bus cannot collapse the layout. It still counts toward HPWL.
MAX_NET_FANOUT: int = 16

#: Rotations are quantised to this quantum (degrees) when computing the
#: orientation-consistency metric and (in P3) when normalising orientations.
ORIENT_ROTATION_QUANTUM_DEG: float = 90.0

# ---------------------------------------------------------------------------
# Part classification tunables (P2 — REQ-CLASS-002/003)
# ---------------------------------------------------------------------------

#: A non-connector footprint with at least this many pads is classified as an
#: integrated circuit (the anchor of a decoupling-cap cluster).
IC_PAD_THRESHOLD: int = 4

#: Reference-designator prefixes that mark a part as a connector. Connectors are
#: never reclassified as ICs regardless of pad count (REQ-CLASS-002).
CONNECTOR_PREFIXES: tuple[str, ...] = ("J", "P", "CN", "X")

#: Reference-designator prefixes that mark a part as a crystal / oscillator
#: (REQ-CLASS-003). ``X`` is only a crystal when it is not already a connector.
CRYSTAL_PREFIXES: tuple[str, ...] = ("Y",)

# ---------------------------------------------------------------------------
# Clustering tunables (P2 — REQ-CLUSTER-002)
# ---------------------------------------------------------------------------

#: A part-graph edge below this weight does not bind two footprints into the
#: same connectivity cluster (weak/incidental connections do not group blocks).
CLUSTER_WEIGHT_FLOOR: float = 0.25

# ---------------------------------------------------------------------------
# Decoupling-cap pairing tunables (P2 — REQ-DECAP-003)
# ---------------------------------------------------------------------------

#: Maximum centre-to-centre distance (mm) a decoupling cap may sit from the IC
#: it decouples. Beyond this the cap falls back to ordinary proximity placement
#: (never-worse fallback, REQ-DECAP-005).
DECAP_MAX_MM: float = 3.0

# ---------------------------------------------------------------------------
# Constructive-placement / legalizer tunables (P2 — REQ-PROX-003, REQ-LEGAL-002)
# ---------------------------------------------------------------------------

#: Number of bounded local-improvement passes after constructive placement. Each
#: pass only accepts a move on a strict HPWL decrease, so the search terminates.
PROX_IMPROVE_PASSES: int = 2

#: Candidate-offset grid step (mm) for the constructive / improvement search.
PROX_CANDIDATE_GRID_MM: float = 0.5

#: Maximum legalizer passes before it stops and reports any residual overlaps as
#: a structured warning rather than looping (REQ-LEGAL-002).
LEGALIZE_MAX_PASSES: int = 40

# ---------------------------------------------------------------------------
# Signal-flow ordering tunables (P3 — REQ-FLOW-001)
# ---------------------------------------------------------------------------

#: Which board axis carries signal flow when the board is square / the anchored
#: input+output connectors do not clearly imply an axis. ``"x"`` = flow left→
#: right (the wider board default), ``"y"`` = flow top→bottom.
FLOW_AXIS_TIE_BREAK: str = "x"

#: Case-insensitive substrings that mark a connector's net as a **board input**
#: (power-in / primary source). A connector whose pads carry any of these is an
#: input endpoint for flow ordering (REQ-FLOW-001). Named, overridable — no inline
#: magic strings.
FLOW_INPUT_NET_TOKENS: tuple[str, ...] = (
    "USB", "VBUS", "VIN", "DCIN", "DC_IN", "PWR_IN", "PWRIN", "SOURCE", "LINE_IN",
    "LINEIN",
)

#: Case-insensitive substrings that mark a connector's net as a **board output**
#: (load / sink). A connector whose pads carry any of these is an output endpoint
#: for flow ordering (REQ-FLOW-001).
FLOW_OUTPUT_NET_TOKENS: tuple[str, ...] = (
    "VOUT", "OUT", "LOAD", "SPK", "SPEAKER", "HP", "HEADPHONE", "LINE_OUT",
    "LINEOUT",
)

# ---------------------------------------------------------------------------
# Sensitive-net proximity (P4 — REQ-SENSE-001..003)
# ---------------------------------------------------------------------------

#: Maximum centre-to-centre distance (mm) a crystal / oscillator may sit from
#: the IC it clocks. Beyond this the crystal falls back to ordinary proximity
#: placement (never-worse fallback, REQ-SENSE-001, mirrors ``DECAP_MAX_MM``).
SENSE_MAX_MM: float = 5.0

#: Part-graph weight multiplier applied to a net declared as a differential pair
#: (via ``.kicad_pro`` netclass ``diff_pair_*`` data). Pulls the pair's endpoint
#: footprints tight so the constructive placer + improver shorten the pair
#: (REQ-SENSE-002). No new declaration surface — P4 only reads the netclass data.
DIFFPAIR_WEIGHT_MULT: float = 4.0

#: A net whose name matches this pattern (CLK / *_CLK / XTAL* / OSC*) is a
#: clock-like net and gets ``CLOCK_WEIGHT_MULT`` proximity weight — between a
#: normal 2-pin signal (×1) and a wide bus (×0), so clock distribution stays
#: compact without dominating (REQ-SENSE-003).
CLOCK_NET_PATTERN: re.Pattern[str] = re.compile(
    r"^(.*_)?(CLK|XTAL|OSC)\w*$", re.IGNORECASE,
)

#: Weight multiplier for clock-like nets (REQ-SENSE-003).
CLOCK_WEIGHT_MULT: float = 1.5

# ---------------------------------------------------------------------------
# Placement-quality gate thresholds (P4 — REQ-GATE-001; advisory unless promoted)
# ---------------------------------------------------------------------------

#: Total-HPWL ceiling (mm) for the placement-quality gate. ``None`` = advisory
#: only / no ceiling by default (Q5 lean: HPWL advisory). When set and exceeded,
#: it appears in ``violations`` as ``severity: "advisory"`` and blocks only if
#: ``GATE_PROMOTE_ADVISORY`` is true.
GATE_HPWL_MAX_MM: float | None = None

#: Decap-distance advisory ceiling (mm); reuses the P2 target by default.
GATE_DECAP_MAX_MM: float = DECAP_MAX_MM

#: When true, advisory violations (HPWL / decap distance) become hard fails so an
#: operator can enforce a wire-length / decoupling budget without code edits
#: (REQ-CFG-002). Overlaps / out-of-outline are always blocking regardless.
GATE_PROMOTE_ADVISORY: bool = False


# ---------------------------------------------------------------------------
# Override hook (REQ-CFG-002)
# ---------------------------------------------------------------------------

#: Tunables that ``load_overrides`` is allowed to replace at run time. Only these
#: keys are honoured; anything else in the override mapping is ignored so a typo
#: cannot silently disable a gate. Values must match the module-level type.
_OVERRIDABLE: frozenset[str] = frozenset({
    "MAX_NET_FANOUT",
    "IC_PAD_THRESHOLD",
    "CLUSTER_WEIGHT_FLOOR",
    "DECAP_MAX_MM",
    "PROX_IMPROVE_PASSES",
    "PROX_CANDIDATE_GRID_MM",
    "LEGALIZE_MAX_PASSES",
    "FLOW_AXIS_TIE_BREAK",
    "FLOW_INPUT_NET_TOKENS",
    "FLOW_OUTPUT_NET_TOKENS",
    "SENSE_MAX_MM",
    "DIFFPAIR_WEIGHT_MULT",
    "CLOCK_WEIGHT_MULT",
    "GATE_HPWL_MAX_MM",
    "GATE_DECAP_MAX_MM",
    "GATE_PROMOTE_ADVISORY",
})


def load_overrides() -> dict[str, object]:
    """Return per-board overrides for the placement tunables.

    Default: a no-op that returns an empty mapping — the module-level constants
    above are authoritative. A deployment can monkeypatch this function (or a
    future config loader can replace it) to return a mapping of constant name ->
    value; ``get_tunable`` consults it, restricted to ``_OVERRIDABLE`` keys.
    """
    return {}


def get_tunable(name: str) -> object:
    """Resolve a placement tunable, honouring ``load_overrides`` (REQ-CFG-002).

    Looks the override mapping up first (only for keys in ``_OVERRIDABLE``);
    falls back to the module-level constant of the same name. Raises
    ``KeyError`` if ``name`` is not a defined tunable, so a typo fails loudly
    rather than returning ``None``.
    """
    if name in _OVERRIDABLE:
        overrides = load_overrides()
        if name in overrides:
            return overrides[name]
    if name not in globals():
        raise KeyError(f"unknown placement tunable: {name!r}")
    return globals()[name]


def get_int(name: str) -> int:
    """``get_tunable`` narrowed to ``int`` (for numeric tunables)."""
    return int(cast("SupportsInt", get_tunable(name)))


def get_float(name: str) -> float:
    """``get_tunable`` narrowed to ``float`` (for distance/weight tunables)."""
    return float(cast("SupportsFloat", get_tunable(name)))


def get_str(name: str) -> str:
    """``get_tunable`` narrowed to ``str`` (for the flow-axis tie-break)."""
    return str(get_tunable(name))


def get_bool(name: str) -> bool:
    """``get_tunable`` narrowed to ``bool`` (for the gate-promotion flag)."""
    return bool(get_tunable(name))


def get_float_or_none(name: str) -> float | None:
    """``get_tunable`` for a nullable distance ceiling (``GATE_HPWL_MAX_MM``)."""
    value = get_tunable(name)
    if value is None:
        return None
    return float(cast("SupportsFloat", value))


def is_clock_net(net_name: str) -> bool:
    """True for clock-like nets (CLK / *_CLK / XTAL* / OSC*, REQ-SENSE-003)."""
    name = net_name.strip()
    if not name:
        return False
    return CLOCK_NET_PATTERN.match(name) is not None


def classify_net(net_name: str) -> str:
    """Classify a net as ``"power"`` or ``"signal"`` (REQ-GRAPH-003).

    A net is power/ground iff its name matches ``POWER_NET_EXACT``
    (case-insensitive exact) or ``POWER_NET_PATTERN`` (voltage rail). Everything
    else, including ``VIN`` / ``VOUT`` by default, is signal.
    """
    name = net_name.strip()
    if not name:
        return "signal"
    if name.upper() in POWER_NET_EXACT:
        return "power"
    if POWER_NET_PATTERN.match(name):
        return "power"
    return "signal"
