"""Variant authoring tools — 4 tools (K10-S4, REQ-S4B-001..009).

The authoring counterpart to the K10-S3 variant-aware exports: define
variants and per-component overrides from the MCP, headlessly, on the
project files themselves. Mutations are capability-gated on the serving
kicad-cli (``variant-authoring``) and go through
:class:`~kicad_mcp.utils.variant_files.VariantWriteSession` —
write-validate-commit with kicad-cli oracles, atomic, stale-guarded.

Tool bodies are module-level ``run_*`` functions (unit-testable without a
FastMCP instance); ``register_tools`` wraps them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.variant_files import (
    PCB_MIN_FORMAT,
    SCH_MIN_FORMAT,
    ProjectFiles,
    VariantAuthorError,
    VariantOverrides,
    VariantWriteError,
    VariantWriteSession,
    check_format_versions,
    file_format_version,
    pcb_list_board_variants,
    pcb_list_footprint_overrides,
    pcb_remove_board_variant,
    pcb_remove_footprint_variants,
    pcb_set_footprint_variant,
    pcb_upsert_board_variant,
    pro_list_variants,
    pro_remove_variant,
    pro_upsert_variant,
    resolve_project,
    sch_list_overrides,
    sch_remove_variant,
    sch_set_variant,
)

logger = get_logger("tools.variants")

_SCH_ONLY_KEYS = {"exclude_from_sim"}


def _authoring_gate() -> tuple[Path | None, str | None]:
    """Resolve the serving kicad-cli and gate on ``variant-authoring``.

    Returns ``(cli_path, None)`` to proceed, or ``(None, error_json)`` —
    the REQ-S4B-005 structured refusal, raised before any file is touched.
    """
    from kicad_mcp.models.errors import KicadCapabilityError
    from kicad_mcp.utils.kicad_capabilities import requires_kicad
    from kicad_mcp.utils.platform_helper import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        return None, json.dumps({
            "status": "error",
            "error": "capability_unavailable",
            "capability": "variant-authoring",
            "required": "kicad-cli 10.0+ (variant-capable)",
            "detected": "kicad-cli not found",
            "hint": "Install KiCad 10 and ensure kicad-cli is discoverable.",
        }, indent=2)
    try:
        requires_kicad("variant-authoring", cli)
    except KicadCapabilityError as exc:
        return None, json.dumps({"status": "error", **exc.payload}, indent=2)
    return cli, None


def _error(payload: dict[str, Any]) -> str:
    return json.dumps({"status": "error", **payload}, indent=2)


def _pcbnew_note() -> str | None:
    from kicad_mcp.utils.platform_helper import is_pcbnew_running

    if is_pcbnew_running():
        return (
            "pcbnew appears to be running — variant changes were written to "
            "disk; reload the board from disk (or reopen the project) to see them."
        )
    return None


def _normalize_members(members: dict[str, Any], side: str) -> dict[str, Any]:
    """One side's raw block members → the positive-logic display form."""
    out: dict[str, Any] = {}
    if "dnp" in members:
        out["dnp"] = members["dnp"]
    if side == "sch":
        for key in ("in_bom", "in_pos_files", "exclude_from_sim"):
            if key in members:
                out[key] = members[key]
    else:
        if "exclude_from_bom" in members:
            out["in_bom"] = not members["exclude_from_bom"]
        if "exclude_from_pos_files" in members:
            out["in_pos_files"] = not members["exclude_from_pos_files"]
    if members.get("fields"):
        out["fields"] = dict(members["fields"])
    return out


def _modern(path: Path | None, minimum: int) -> bool:
    if path is None:
        return False
    stamp = file_format_version(path.read_text(encoding="utf-8"))
    return stamp is not None and stamp >= minimum


def build_variant_listing(files: ProjectFiles) -> dict[str, Any]:
    """The merged three-file variant view + cross-file inconsistency flags."""
    pro_text = files.pro.read_text(encoding="utf-8")
    registry = pro_list_variants(pro_text)

    sch_ok = _modern(files.sch, SCH_MIN_FORMAT)
    pcb_ok = _modern(files.pcb, PCB_MIN_FORMAT)
    sch_overrides = (
        sch_list_overrides(files.sch.read_text(encoding="utf-8"))
        if sch_ok and files.sch is not None else {}
    )
    pcb_text = files.pcb.read_text(encoding="utf-8") if pcb_ok and files.pcb else None
    board_registry = pcb_list_board_variants(pcb_text) if pcb_text else []
    fp_overrides = pcb_list_footprint_overrides(pcb_text) if pcb_text else {}

    pro_names = [v["name"] for v in registry]
    board_names = [v["name"] for v in board_registry]
    override_names: set[str] = set()
    for per_ref in sch_overrides.values():
        override_names.update(per_ref)
    for per_ref in fp_overrides.values():
        override_names.update(per_ref)

    inconsistencies: list[str] = []
    all_names = list(dict.fromkeys(pro_names + board_names + sorted(override_names)))
    variants: list[dict[str, Any]] = []
    for name in all_names:
        entry: dict[str, Any] = {"name": name}
        for source in (registry, board_registry):
            for reg_entry in source:
                if reg_entry["name"] == name and reg_entry.get("description"):
                    entry.setdefault("description", reg_entry["description"])
        entry["in_pro_registry"] = name in pro_names
        if pcb_text is not None:
            entry["in_board_registry"] = name in board_names
            if (name in pro_names) != (name in board_names):
                where, missing = (
                    (".kicad_pro", "board") if name in pro_names else ("board", ".kicad_pro")
                )
                inconsistencies.append(
                    f"variant {name!r} is registered in the {where} registry but "
                    f"missing from the {missing} registry"
                )
        if name in override_names and name not in pro_names:
            inconsistencies.append(
                f"variant {name!r} has component overrides but no "
                f".kicad_pro registry entry"
            )

        components: dict[str, Any] = {}
        refs = set(sch_overrides) | set(fp_overrides)
        for ref in sorted(refs):
            sch_m = sch_overrides.get(ref, {}).get(name)
            pcb_m = fp_overrides.get(ref, {}).get(name)
            if sch_m is None and pcb_m is None:
                continue
            sides = [s for s, m in (("sch", sch_m), ("pcb", pcb_m)) if m is not None]
            merged: dict[str, Any] = {}
            if pcb_m is not None:
                merged.update(_normalize_members(pcb_m, "pcb"))
            if sch_m is not None:
                merged.update(_normalize_members(sch_m, "sch"))
            components[ref] = {"overrides": merged, "sides": sides}
            if files.sch is not None and files.pcb is not None and len(sides) == 1:
                only = _normalize_members(sch_m or pcb_m or {}, sides[0])
                if set(only) - _SCH_ONLY_KEYS - {"fields"} or only.get("fields"):
                    inconsistencies.append(
                        f"override for {ref} in variant {name!r} exists only on "
                        f"the {sides[0]} side"
                    )
            elif sch_m is not None and pcb_m is not None:
                norm_sch = _normalize_members(sch_m, "sch")
                norm_pcb = _normalize_members(pcb_m, "pcb")
                for key in set(norm_sch) & set(norm_pcb):
                    if norm_sch[key] != norm_pcb[key]:
                        inconsistencies.append(
                            f"{ref} in variant {name!r}: sch and pcb disagree on "
                            f"{key} ({norm_sch[key]!r} vs {norm_pcb[key]!r})"
                        )
        if components:
            entry["components"] = components
        variants.append(entry)

    return {
        "variants": variants,
        "files": {
            "pro": files.pro.name,
            "sch": files.sch.name if files.sch else "absent",
            "pcb": files.pcb.name if files.pcb else "absent",
        },
        "inconsistencies": inconsistencies,
    }


# ── module-level tool bodies (unit-testable) ─────────────────────────────────

def run_list_variants(project_path: str) -> str:
    try:
        files = resolve_project(Path(project_path))
        return json.dumps(
            {"status": "success", **build_variant_listing(files)}, indent=2
        )
    except VariantAuthorError as exc:
        return _error(exc.payload)
    except (OSError, json.JSONDecodeError) as exc:
        return _error({"message": f"failed to read project: {exc}"})


def run_create_variant(
    project_path: str,
    name: str,
    description: str | None,
    change_log: ChangeLog | None = None,
) -> str:
    if not name or not name.strip():
        return _error({"error": "parameter_error",
                       "message": "variant name must be non-empty"})
    name = name.strip()
    cli, refusal = _authoring_gate()
    if refusal is not None:
        return refusal
    assert cli is not None
    try:
        files = resolve_project(Path(project_path))
        fmt = check_format_versions(files)
        if fmt is not None:
            return _error(fmt)
        session = VariantWriteSession(files)
        changes: dict[str, str] = {}
        if session.stage(files.pro, pro_upsert_variant(
                session.texts[files.pro], name, description)):
            changes["pro"] = "registry entry added/updated"
        if files.pcb is not None:
            if session.stage(files.pcb, pcb_upsert_board_variant(
                    session.texts[files.pcb], name, description)):
                changes["pcb"] = "board registry entry added/updated"
        else:
            changes["pcb"] = "absent"
        commit = session.commit(cli)
        if change_log is not None:
            change_log.record("create_variant", {
                "project": project_path, "name": name, "description": description,
            })
        result: dict[str, Any] = {
            "status": "success", "variant": name, "changes": changes, **commit,
        }
        if not commit["changed"]:
            result["message"] = f"variant {name!r} already exists as requested"
        note = _pcbnew_note()
        if note and commit["changed"]:
            result["note"] = note
        return json.dumps(result, indent=2)
    except (VariantAuthorError, VariantWriteError) as exc:
        return _error(exc.payload)
    except (OSError, json.JSONDecodeError) as exc:
        return _error({"message": f"create_variant failed: {exc}"})


def run_delete_variant(
    project_path: str, name: str, change_log: ChangeLog | None = None
) -> str:
    cli, refusal = _authoring_gate()
    if refusal is not None:
        return refusal
    assert cli is not None
    try:
        files = resolve_project(Path(project_path))
        fmt = check_format_versions(files)
        if fmt is not None:
            return _error(fmt)
        session = VariantWriteSession(files)
        changes: dict[str, Any] = {}
        if session.stage(files.pro, pro_remove_variant(
                session.texts[files.pro], name)):
            changes["pro"] = "registry entry removed"
        if files.sch is not None:
            new_sch, removed = sch_remove_variant(session.texts[files.sch], name)
            if session.stage(files.sch, new_sch):
                changes["sch"] = f"{removed} symbol override block(s) removed"
        else:
            changes["sch"] = "absent"
        if files.pcb is not None:
            new_pcb = pcb_remove_board_variant(session.texts[files.pcb], name)
            new_pcb, fp_removed = pcb_remove_footprint_variants(new_pcb, name)
            if session.stage(files.pcb, new_pcb):
                changes["pcb"] = (
                    f"board registry entry + {fp_removed} footprint "
                    f"override block(s) removed"
                )
        else:
            changes["pcb"] = "absent"
        commit = session.commit(cli)
        if change_log is not None:
            change_log.record("delete_variant", {"project": project_path, "name": name})
        result: dict[str, Any] = {
            "status": "success", "variant": name, "changes": changes, **commit,
        }
        if not commit["changed"]:
            result["message"] = f"variant {name!r} not present — nothing to delete"
        note = _pcbnew_note()
        if note and commit["changed"]:
            result["note"] = note
        return json.dumps(result, indent=2)
    except (VariantAuthorError, VariantWriteError) as exc:
        return _error(exc.payload)
    except (OSError, json.JSONDecodeError) as exc:
        return _error({"message": f"delete_variant failed: {exc}"})


def run_set_variant_overrides(
    project_path: str,
    variant: str,
    reference: str,
    dnp: bool | None = None,
    in_bom: bool | None = None,
    in_pos_files: bool | None = None,
    exclude_from_sim: bool | None = None,
    fields: dict[str, str] | None = None,
    change_log: ChangeLog | None = None,
) -> str:
    cli, refusal = _authoring_gate()
    if refusal is not None:
        return refusal
    assert cli is not None
    try:
        files = resolve_project(Path(project_path))
        fmt = check_format_versions(files)
        if fmt is not None:
            return _error(fmt)
        session = VariantWriteSession(files)
        known = [v["name"] for v in pro_list_variants(session.texts[files.pro])]
        if variant not in known:
            return _error({
                "error": "parameter_error",
                "message": (
                    f"variant {variant!r} is not defined in this project "
                    f"(known: {known or 'none'}); call create_variant first"
                ),
            })
        overrides = VariantOverrides(
            dnp=dnp, in_bom=in_bom, in_pos_files=in_pos_files,
            exclude_from_sim=exclude_from_sim, fields=fields or {},
        )
        changes: dict[str, str] = {}
        if files.sch is not None:
            if session.stage(files.sch, sch_set_variant(
                    session.texts[files.sch], reference, variant, overrides)):
                changes["sch"] = "symbol override block updated"
        else:
            changes["sch"] = "absent"
        if files.pcb is not None:
            if session.stage(files.pcb, pcb_set_footprint_variant(
                    session.texts[files.pcb], reference, variant, overrides)):
                changes["pcb"] = "footprint override block updated"
            if exclude_from_sim is not None and files.sch is None:
                changes["note_exclude_from_sim"] = (
                    "exclude_from_sim has no board-side token; project has "
                    "no schematic, so it was not recorded"
                )
        else:
            changes["pcb"] = "absent"
        commit = session.commit(cli)
        if change_log is not None:
            change_log.record("set_variant_overrides", {
                "project": project_path, "variant": variant, "reference": reference,
                "dnp": dnp, "in_bom": in_bom, "in_pos_files": in_pos_files,
                "exclude_from_sim": exclude_from_sim, "fields": fields,
            })
        result: dict[str, Any] = {
            "status": "success", "variant": variant, "reference": reference,
            "changes": changes, **commit,
        }
        if not commit["changed"]:
            result["message"] = "overrides already match — nothing to write"
        note = _pcbnew_note()
        if note and commit["changed"]:
            result["note"] = note
        return json.dumps(result, indent=2)
    except (VariantAuthorError, VariantWriteError) as exc:
        return _error(exc.payload)
    except (OSError, json.JSONDecodeError) as exc:
        return _error({"message": f"set_variant_overrides failed: {exc}"})


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register variant authoring tools on the MCP server."""

    @mcp.tool()
    def list_variants(project_path: str) -> str:
        """List design variants defined in a KiCad project (KiCad 10 format).

        Read-only — works on any KiCad version. Merges all three project
        files (.kicad_pro registry, schematic symbol overrides, board
        registry + footprint overrides) and flags cross-file inconsistencies.

        Args:
            project_path: Path to the .kicad_pro (or a sibling .kicad_sch /
                .kicad_pcb — the project is resolved by stem).

        Returns:
            JSON: variants with descriptions and per-component overrides
            (positive logic: dnp, in_bom, in_pos_files, exclude_from_sim,
            fields), plus any inconsistencies between the three files.
        """
        return run_list_variants(project_path)

    @mcp.tool()
    def create_variant(
        project_path: str, name: str, description: str | None = None
    ) -> str:
        """Create a design variant in a KiCad 10 project.

        Registers the variant in the .kicad_pro and (when a board exists)
        the .kicad_pcb board registry — both sides in one operation. Writes
        are validated with kicad-cli before the originals are replaced;
        a failed validation leaves every file byte-identical. Requires a
        variant-capable KiCad 10 (structured refusal on KiCad 9).

        Args:
            project_path: Path to the .kicad_pro (or a sibling project file).
            name: Variant name (e.g. "Lite"). Must be non-empty.
            description: Optional human-readable description.

        Returns:
            JSON with the per-file changes and oracle validation summary.
        """
        return run_create_variant(project_path, name, description, change_log)

    @mcp.tool()
    def delete_variant(project_path: str, name: str) -> str:
        """Delete a design variant from a KiCad 10 project — fully.

        Removes the registry entries (.kicad_pro and board) AND every
        per-component override block carrying that variant name, on both the
        schematic and board sides — no orphan tokens. Deleting a variant
        that doesn't exist is a no-op, not an error. Validated + atomic like
        all variant writes. Requires KiCad 10 (structured refusal on 9).

        Args:
            project_path: Path to the .kicad_pro (or a sibling project file).
            name: Variant name to delete.

        Returns:
            JSON with per-file removal counts and validation summary.
        """
        return run_delete_variant(project_path, name, change_log)

    @mcp.tool()
    def set_variant_overrides(
        project_path: str,
        variant: str,
        reference: str,
        dnp: bool | None = None,
        in_bom: bool | None = None,
        in_pos_files: bool | None = None,
        exclude_from_sim: bool | None = None,
        fields: dict[str, str] | None = None,
    ) -> str:
        """Set per-variant overrides on one component, both sides in one op.

        Writes the override onto the schematic symbol AND the board footprint
        (KiCad's "transfer variants to the PCB editor" flow) so BOM, pos, and
        gerber exports for the variant are coherent with zero extra steps.
        All params use positive logic (in_bom=False → excluded from BOM);
        the board file's inverted tokens are handled internally. None leaves
        an attribute untouched; setting a value equal to the component's base
        attribute removes the override (KiCad's omit-when-equal convention).
        Requires KiCad 10 (structured refusal on 9); validated + atomic.

        Args:
            project_path: Path to the .kicad_pro (or a sibling project file).
            variant: Variant name — must already exist (create_variant first).
            reference: Component reference designator (e.g. "R1").
            dnp: Do-not-populate for this variant.
            in_bom: Include in BOM for this variant.
            in_pos_files: Include in pick-and-place files for this variant.
            exclude_from_sim: Exclude from simulation (schematic side only).
            fields: Per-variant field value overrides, e.g. {"Value": "4.7k"}.

        Returns:
            JSON with the per-file changes and oracle validation summary.
        """
        return run_set_variant_overrides(
            project_path, variant, reference, dnp, in_bom, in_pos_files,
            exclude_from_sim, fields, change_log,
        )
