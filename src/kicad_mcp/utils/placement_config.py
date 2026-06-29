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
# Override hook (REQ-CFG-002)
# ---------------------------------------------------------------------------

def load_overrides() -> dict[str, object]:
    """Return per-board overrides for the placement tunables.

    P1 default: a no-op that returns an empty mapping. The hook exists so P2 can
    wire it to the existing config mechanism when placement first consumes
    config; until then the module-level constants above are authoritative.
    """
    return {}


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
