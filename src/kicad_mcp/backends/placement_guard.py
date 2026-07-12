"""Shared duplicate-reference placement guard (F2 REQ-DUP, defect #16).

Placement paths historically appended a footprint without checking whether the
reference already existed, silently creating duplicate-ref boards where every
by-ref operation resolves to an arbitrary copy. This module holds the single
shared rule; each backend (file / bridge client / IPC) supplies the existing-ref
lookup from its own board model and defers here (REQ-DUP-1).

Outcomes:
- ``"create"``      — the reference is new; proceed with placement.
- ``"idempotent"``  — an identical placement already exists (same lib_id,
  position within ``POSITION_TOL_MM``, rotation equal mod 360, same layer);
  return success WITHOUT adding anything (REQ-DUP-2).
- ``DuplicateRefError`` — the reference exists but the placement differs;
  structured refusal carrying the existing state and the suggested tool
  (REQ-DUP-3). No duplicate is ever created, and there is no opt-out flag
  (REQ-DUP-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

# REQ-DUP-5 (F2-Q1): 1000 KiCad internal units at 1 nm — half a 25.4 µm grid
# step. Absorbs float round-trips through the sexp writer without masking a
# real move. Rotation is compared exact-mod-360; layer exact.
POSITION_TOL_MM = 0.0127


@dataclass(frozen=True)
class ExistingComponent:
    """State of the footprint already on the board with the requested ref."""

    reference: str
    lib_id: str
    x: float
    y: float
    rotation: float
    layer: str


class DuplicateRefError(Exception):
    """Informative refusal (REQ-DUP-3): ref exists with a different placement."""

    def __init__(self, existing: ExistingComponent, suggested_tool: str) -> None:
        self.existing = existing
        self.suggested_tool = suggested_tool
        super().__init__(
            f"Reference {existing.reference!r} already exists on the board: "
            f"{existing.lib_id} at ({existing.x}, {existing.y}) "
            f"rotation {existing.rotation} on {existing.layer}. "
            f"Placing it again would create a silent duplicate — "
            f"use {suggested_tool} instead."
        )

    def to_refusal(self) -> dict[str, Any]:
        """The structured tool-level refusal payload (REQ-DUP-3)."""
        return {
            "status": "refused",
            "reason": str(self),
            "existing": {
                "reference": self.existing.reference,
                "footprint": self.existing.lib_id,
                "x": self.existing.x,
                "y": self.existing.y,
                "rotation": self.existing.rotation,
                "layer": self.existing.layer,
            },
            "suggested_tool": self.suggested_tool,
        }


def check_placement(
    existing: ExistingComponent | None,
    req_ref: str,
    req_lib_id: str,
    req_x: float,
    req_y: float,
    req_rot: float = 0.0,
    req_layer: str = "F.Cu",
) -> Literal["create", "idempotent"]:
    """Apply the duplicate-ref rule (REQ-DUP-1..3). No force flag (REQ-DUP-6)."""
    if existing is None:
        return "create"
    if (
        existing.lib_id == req_lib_id
        and abs(existing.x - req_x) <= POSITION_TOL_MM
        and abs(existing.y - req_y) <= POSITION_TOL_MM
        and (existing.rotation - req_rot) % 360.0 == 0.0
        and existing.layer == req_layer
    ):
        return "idempotent"
    raise DuplicateRefError(
        existing,
        suggested_tool=(
            "move_component"
            if existing.lib_id == req_lib_id
            else "remove_component then place_component (footprint swap)"
        ),
    )


def idempotent_success(existing: ExistingComponent) -> dict[str, Any]:
    """Standard success payload for a matching re-place (REQ-DUP-2)."""
    return {
        "status": "success",
        "idempotent": True,
        "reference": existing.reference,
        "footprint": existing.lib_id,
        "x": existing.x,
        "y": existing.y,
        "rotation": existing.rotation,
        "layer": existing.layer,
        "message": (
            f"{existing.reference} already placed identically — nothing added."
        ),
    }


def existing_from_component(comp: dict[str, Any]) -> ExistingComponent:
    """Adapt a backend ``get_components`` record to :class:`ExistingComponent`.

    Handles both record shapes: the bridge/IPC flat ``x``/``y`` keys and the
    file backend's nested ``position: {x, y}`` dict; ``rotation`` defaults to
    0.0 when absent (the sexp omits a zero rotation).
    """
    pos = comp.get("position")
    if isinstance(pos, dict):
        x = float(pos.get("x", 0.0))
        y = float(pos.get("y", 0.0))
    else:
        x = float(comp.get("x", 0.0))
        y = float(comp.get("y", 0.0))
    return ExistingComponent(
        reference=str(comp.get("reference", "")),
        lib_id=str(comp.get("footprint", "")),
        x=x,
        y=y,
        rotation=float(comp.get("rotation") or 0.0),
        layer=str(comp.get("layer", "F.Cu")),
    )


def index_existing(
    components: Iterable[dict[str, Any]],
) -> dict[str, ExistingComponent]:
    """Map reference → existing component, first occurrence winning.

    First-wins mirrors how by-ref resolution behaves on an already-duplicated
    board, so the guard refuses against the same copy other tools would touch.
    """
    index: dict[str, ExistingComponent] = {}
    for comp in components:
        existing = existing_from_component(comp)
        if existing.reference and existing.reference not in index:
            index[existing.reference] = existing
    return index


def find_batch_duplicate_refs(components: list[dict[str, Any]]) -> list[str]:
    """Refs repeated *within* one bulk input list (REQ-DUP-4).

    A repeated ref means the caller's list is malformed: the whole batch must
    be refused with the board untouched (review clarification, 2026-07-11).
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for comp in components:
        ref = str(comp.get("reference", ""))
        if not ref:
            continue
        if ref in seen and ref not in dupes:
            dupes.append(ref)
        seen.add(ref)
    return dupes
