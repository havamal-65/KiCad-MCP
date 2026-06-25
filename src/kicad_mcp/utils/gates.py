"""Set-time tool gates built on the sidecar validation cache (§6.3).

Generalizes the autoroute "you must have run validate_connector_orientations
first" gate (Phase 6.1.4) into a reusable check any tool can apply: a *gated*
tool refuses (or warns) unless a named *validator* was recorded as having passed
against the file's current byte content.

State lives in the same sidecar `<file>.validation_cache.json` the cache module
already maintains (resolves HLRP §6.3 open-question Q2). Two policies are
offered (resolving Q1 by operation type):

- ``refuse_if_ungated`` — hard refusal, for terminal/expensive operations
  (export_gerbers, autoroute) where running blind wastes work or ships a bad
  artifact.
- ``warn_if_ungated`` — non-blocking warning, for iterative operations
  (sync_schematic_to_pcb) where a hard "run the validator first" on every call
  would wreck the edit loop.

A validator participates simply by calling
``validation_cache.record_validation(file_path, name, result)`` after it runs,
where ``result`` carries a truthy ``passed`` (or the recorder normalizes one).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kicad_mcp.utils.validation_cache import get_validation


def check_gate(file_path: Path, validator_name: str) -> dict[str, Any] | None:
    """Return None if *validator_name* is recorded as passed for *file_path*'s
    current content, else a dict describing the gap.

    The gap dict is ``{"ran": bool, "passed": bool, "violations": [...]}`` —
    ``ran=False`` means no cached result applies to the current file content
    (never run, or the file changed since); ``ran=True, passed=False`` means the
    most recent run failed.
    """
    cached = get_validation(file_path, validator_name)
    if cached is None:
        return {"ran": False, "passed": False, "violations": []}
    if not cached.get("passed"):
        return {
            "ran": True,
            "passed": False,
            "violations": cached.get("violations", []),
        }
    return None


def _gate_message(validator_name: str, gated_tool: str, gap: dict, fix_hint: str) -> str:
    if not gap["ran"]:
        return (
            f"{gated_tool} requires {validator_name} to have passed on the current "
            f"file state first — it has not been run (or the file changed since). "
            f"Call {validator_name}(path), resolve any issues, then retry."
        )
    return (
        f"{gated_tool}: the most recent {validator_name} run did not pass. "
        f"{fix_hint}"
    )


def refuse_if_ungated(
    file_path: Path,
    validator_name: str,
    gated_tool: str,
    *,
    fix_hint: str = "",
) -> str | None:
    """Hard gate: return a ``status:"blocked"`` JSON refusal string if the gate
    fails, else None. For terminal/expensive tools.
    """
    gap = check_gate(file_path, validator_name)
    if gap is None:
        return None
    return json.dumps({
        "status": "blocked",
        "reason": f"{validator_name}_gate",
        "gate": {"validator": validator_name, **gap},
        "message": _gate_message(validator_name, gated_tool, gap, fix_hint),
    }, indent=2)


def warn_if_ungated(
    file_path: Path,
    validator_name: str,
    gated_tool: str,
    *,
    fix_hint: str = "",
) -> dict[str, Any] | None:
    """Soft gate: return a warning dict if the gate fails, else None. For
    iterative tools that should nudge, not block.
    """
    gap = check_gate(file_path, validator_name)
    if gap is None:
        return None
    return {
        "type": f"{validator_name}_not_passed",
        "message": _gate_message(validator_name, gated_tool, gap, fix_hint),
    }
