"""Schematic tools - 22 tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol, BoardOps
from kicad_mcp.backends.file_backend import BoardOpenInKiCadError
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog, create_backup
from kicad_mcp.utils.response_limit import limit_response
from kicad_mcp.utils.validation import (
    validate_kicad_path,
    validate_net_name,
    validate_reference,
    validate_writable_path,
)

logger = get_logger("tools.schematic")


def _format_validator_refusal_message(sf: dict[str, Any]) -> str:
    """One-line summary of a §6.2 validator failure naming up to 3 problems.

    Avoids dumping a large blob into the tool result (the full lists are still
    returned under ``mismatches``/``unresolvable``).
    """
    items: list[str] = []
    for mm in sf.get("mismatches", []):
        items.append(f"{mm['ref']} ({mm['footprint']}) missing pads {mm['missing']}")
    for un in sf.get("unresolvable", []):
        items.append(f"{un['ref']} {un['footprint']!r}: {un['reason']}")
    shown = items[:3]
    overflow = len(items) - len(shown)
    summary = "; ".join(shown)
    if overflow > 0:
        summary += f"; ...and {overflow} more"
    return (
        f"sync_schematic_to_pcb refused: symbol-footprint validation failed. "
        f"{summary}. Run validate_symbol_footprint_pairs to see all issues, then "
        f"fix the symbol's Footprint field or pick a different footprint."
    )


def _schematic_write_refusal(exc: BoardOpenInKiCadError) -> str:
    """Structured refusal for a schematic write while KiCad holds the file
    open (RW-S4 §9); mirrors routing.py's board_open_in_kicad shape."""
    return json.dumps({
        "status": "error",
        "error_code": "schematic_open_in_kicad",
        "lock_file": str(exc.lock_file),
        "lock_holder": exc.lock_holder,
        "message": str(exc),
    }, indent=2)


def suggest_footprint_candidates(
    footprint_lib_id: str,
    *,
    library_ops: Any,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank replacement footprints for an unresolvable ``lib_id`` by name (§6.8).

    Derives a search query from the footprint-name part of *footprint_lib_id* and
    ranks ``library_ops.search_footprints`` hits: exact name → prefix → substring
    (REQ-RANK-002), excluding the input ``lib_id`` itself (REQ-RANK-003), stable
    by ``lib_id`` (REQ-RANK-004), truncated to *limit* (REQ-RANK-003).

    Pure and never raises: any error from the underlying search yields ``[]``
    (REQ-HELP-003), so enriching a sync refusal can never turn it into an error.
    Pad count is captured implicitly via the package-encoded KiCad name, so no
    candidate ``.kicad_mod`` is loaded (Q8-a).
    """
    try:
        name = footprint_lib_id.split(":", 1)[1] if ":" in footprint_lib_id else footprint_lib_id
        name = name.strip()
        query = name

        results = library_ops.search_footprints(query)
        # Package-prefix retry: a wrong metric/handsolder variant still finds its
        # family, e.g. "SOIC-8_3.9x4.9mm_P1.27mm_HandSolder" → also try "SOIC-8".
        if len(results) < limit and "_" in name:
            prefix = name.split("_", 1)[0]
            if prefix and prefix != query:
                seen = {c.get("lib_id") for c in results}
                for c in library_ops.search_footprints(prefix):
                    if c.get("lib_id") not in seen:
                        results.append(c)
                        seen.add(c.get("lib_id"))

        name_l = name.lower()
        query_l = query.lower()

        def tier(c: dict[str, Any]) -> tuple[int, str]:
            n = c.get("name", "").lower()
            if n == name_l:
                return (0, "exact_name")
            if n.startswith(query_l):
                return (1, "prefix")
            return (2, "substring")

        ranked = sorted(
            (c for c in results if c.get("lib_id") != footprint_lib_id),
            key=lambda c: (tier(c)[0], c.get("lib_id", "")),
        )
        return [
            {"lib_id": c["lib_id"], "name": c["name"], "library": c["library"],
             "match_reason": tier(c)[1]}
            for c in ranked[:limit]
        ]
    except Exception as exc:  # noqa: BLE001 — never raise into a sync refusal
        logger.warning("suggest_footprint_candidates failed for %r: %s", footprint_lib_id, exc)
        return []


def _attempt_footprint_swap(
    pcb_p: Path, ref: str, new_fp: str, board_ops: BoardOps | None = None
) -> dict[str, Any]:
    """Swap a PCB footprint for *new_fp*, preserving position/rotation/layer/nets.

    The three mutations (remove → place → re-net) route through *board_ops* — the
    live pcbnew bridge when one is open, so the swap lands on the in-memory board
    and NO reload is needed (#2 deferred work). When *board_ops* is None or the
    file backend, it edits the .kicad_pcb directly (deterministic, CI-testable).
    Reads (current placement, footprint resolution) stay file-side: they are
    read-only and need the fp-lib-table resolver, and file/live board are in sync
    at swap time. Ambiguous cases are skipped with a reason — never guessed.

    Returns {"applied": True, "record": {...}, "via": "bridge"|"file"} on success,
    or {"applied": False, "reason": "..."} when skipped.
    """
    from kicad_mcp.backends.file_backend import (
        FileBoardOps,
        _load_kicad_mod,
        _parse_footprint_bounds,
    )

    file_ops = FileBoardOps(project_dir=pcb_p.parent)
    # Mutations go to the live bridge when one is open; reads stay file-side.
    mutate_ops = board_ops if board_ops is not None else file_ops
    via = "file" if isinstance(mutate_ops, FileBoardOps) else "bridge"

    try:
        state = file_ops.get_component_state(pcb_p, ref)
    except ValueError:
        return {"applied": False, "reason": f"{ref} not found in PCB file"}
    if state.get("locked"):
        return {"applied": False, "reason": "footprint is locked"}
    if state.get("position") is None:
        return {"applied": False, "reason": "footprint has no (at ...) position"}

    new_mod = _load_kicad_mod(new_fp, pcb_p.parent)
    if new_mod is None:
        return {
            "applied": False,
            "reason": f"new footprint '{new_fp}' not found in libraries",
        }

    old_pad_nets: dict[str, str] = state.get("pad_nets", {})
    new_pad_names = {
        str(p.get("number"))
        for p in _parse_footprint_bounds(new_mod)["pads"]
        if p.get("number")
    }
    if old_pad_nets and new_pad_names and not (set(old_pad_nets) & new_pad_names):
        return {
            "applied": False,
            "reason": "pad names incompatible between old and new footprint",
        }

    removed = mutate_ops.remove_component(pcb_p, ref)
    placed = False
    try:
        mutate_ops.place_component(
            pcb_p, ref, new_fp,
            removed["position"]["x"], removed["position"]["y"],
            layer=removed.get("layer") or "F.Cu",
            rotation=removed.get("rotation", 0.0),
        )
        placed = True

        unmatched_pads: list[dict[str, str]] = []
        for pad_name, net in sorted(old_pad_nets.items()):
            if new_pad_names and pad_name not in new_pad_names:
                unmatched_pads.append({"pad": pad_name, "net": net})
                continue
            try:
                mutate_ops.assign_net(pcb_p, ref, pad_name, net)
            except ValueError:
                unmatched_pads.append({"pad": pad_name, "net": net})
    except Exception as exc:
        # SW-1 C4: a failure after a successful file-side remove would
        # otherwise delete the part from the board — restore the captured
        # block verbatim. Bridge-leg exceptions propagate exactly as before
        # (OQ2 carve-out: no raw_block there).
        raw_block = removed.get("raw_block")
        if via == "file" and raw_block:
            if placed:
                # RB-RES1: the new footprint block already landed in the
                # file — remove it first, or the verbatim restore below
                # would re-insert the old block ALONGSIDE it (duplicate
                # reference). If this cleanup fails, report rollback
                # failure rather than create the duplicate.
                try:
                    file_ops.remove_component(pcb_p, ref)
                except Exception as cleanup_exc:  # noqa: BLE001 — reported, never raised
                    return {
                        "applied": False,
                        "reason": (
                            f"swap failed: {exc}; ROLLBACK FAILED: could not"
                            f" remove the half-placed new footprint:"
                            f" {cleanup_exc} — restore {ref} from the sync"
                            f" backup"
                        ),
                        "restored": False,
                    }
            try:
                file_ops.restore_footprint_block(pcb_p, raw_block)
            except Exception as restore_exc:  # noqa: BLE001 — reported, never raised
                return {
                    "applied": False,
                    "reason": (
                        f"swap failed: {exc}; ROLLBACK FAILED: {restore_exc}"
                        f" — restore {ref} from the sync backup"
                    ),
                    "restored": False,
                }
            return {
                "applied": False,
                "reason": (
                    f"swap failed: {exc}; original footprint restored "
                    f"(rollback)"
                ),
                "restored": True,
            }
        raise

    return {
        "applied": True,
        "via": via,
        "record": {
            "reference": ref,
            "old_footprint": removed["footprint"],
            "new_footprint": new_fp,
            "position": removed["position"],
            "rotation": removed.get("rotation", 0.0),
            "nets_reassigned": len(old_pad_nets) - len(unmatched_pads),
            "unmatched_pads": unmatched_pads,
        },
    }


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register schematic tools on the MCP server."""

    @mcp.tool()
    def read_schematic(path: str, include: list[str] | None = None) -> str:
        """Read a KiCad schematic and return its structure.

        Args:
            path: Path to .kicad_sch file.
            include: Optional list of sections to return. Omit for all sections.
                     Valid values: symbols, wires, labels, no_connects, junctions, sheets.
                     The "info" section is always returned regardless of this filter.

        Returns:
            JSON with schematic info, symbols, wires, and labels.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        ops = backend.get_schematic_ops()
        result = ops.read_schematic(p)

        VALID = {"symbols", "wires", "labels", "no_connects", "junctions", "sheets"}
        if include:
            keep = set(include) & VALID
            result = {k: v for k, v in result.items() if k == "info" or k in keep}

        change_log.record("read_schematic", {"path": path})
        return json.dumps({"status": "success", **limit_response(result)}, indent=2)

    @mcp.tool()
    def get_sheet_hierarchy(path: str) -> str:
        """Read the hierarchical sheet tree from a root schematic.

        Recursively reads the root schematic and all sub-sheets to build
        a tree structure showing the design hierarchy. Useful for complex
        designs with multiple sub-sheets.

        Args:
            path: Path to the root .kicad_sch file.

        Returns:
            JSON with tree structure: {name, file, symbols_count, wires_count, sheets: [children]}.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        ops = backend.get_schematic_ops()
        try:
            result = ops.get_sheet_hierarchy(p)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Sheet hierarchy queries not supported by current backend.",
            })
        change_log.record("get_sheet_hierarchy", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def create_schematic(
        path: str,
        title: str = "",
        revision: str = "",
    ) -> str:
        """Create a new, empty KiCad schematic file.

        Generates a minimal valid KiCad 8+ schematic with proper structure
        including version, generator, UUID, paper size, lib_symbols, and
        sheet_instances. Components can then be added with add_component.

        Args:
            path: Path for the new .kicad_sch file. Parent directory must exist.
            title: Optional schematic title for the title block.
            revision: Optional revision string for the title block.

        Returns:
            JSON with created file path and UUID.
        """
        p = validate_writable_path(path, ".kicad_sch")
        if p.exists():
            return json.dumps({
                "status": "error",
                "message": f"File already exists: {p}. Use read_schematic to work with existing files.",
            })
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.create_schematic(p, title=title, revision=revision)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Schematic creation not supported by current backend.",
            })
        change_log.record(
            "create_schematic",
            {"path": path, "title": title, "revision": revision},
            file_modified=path,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_component(
        path: str,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        mirror: str | None = None,
        footprint: str = "",
        properties: dict[str, str] | None = None,
        unit: int = 1,
        in_bom: bool = True,
        on_board: bool = True,
        dnp: bool = False,
        exclude_from_sim: bool = False,
    ) -> str:
        """Add a component symbol to the schematic.

        Args:
            path: Path to .kicad_sch file.
            lib_id: Library symbol identifier (e.g. 'Device:R', 'MCU_Microchip:ATmega328P-AU').
            reference: Reference designator (e.g. R1, U1).
            value: Component value (e.g. '10k', '100nF').
            x: X position in schematic units (mm).
            y: Y position in schematic units (mm).
            rotation: Rotation in degrees (0, 90, 180, 270).
            mirror: Mirror axis - 'x' or 'y', or None for no mirror.
            footprint: Footprint lib_id (e.g. 'Resistor_SMD:R_0402_1005Metric').
            properties: Additional properties dict (e.g. {'Datasheet': '...', 'MPN': '...'}).
            unit: Which unit of a multi-unit symbol to place (default 1).
                  To place another unit of the same component, call again with
                  the SAME reference and lib_id and a different unit — e.g. a
                  dual op-amp is U1 unit 1 plus U1 unit 2. The same
                  (reference, unit) twice, a unit beyond the symbol's unit
                  count, or one reference across two lib_ids is refused.
            in_bom: Symbol appears in the bill of materials (default True).
            on_board: Symbol has a board footprint (default True).
            dnp: Do-not-populate flag (default False).
            exclude_from_sim: Ignored by the simulator (default False).

        Returns:
            JSON with placed component details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_component(
                p, lib_id, reference, value, x, y,
                rotation=rotation, mirror=mirror,
                footprint=footprint, properties=properties,
                unit=unit,
                in_bom=in_bom, on_board=on_board,
                dnp=dnp, exclude_from_sim=exclude_from_sim,
            )
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "add_component",
            {"path": path, "lib_id": lib_id, "reference": reference,
             "value": value, "unit": unit},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_wire(
        path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> str:
        """Add a wire connection between two points in the schematic.

        Args:
            path: Path to .kicad_sch file.
            start_x: Start X coordinate.
            start_y: Start Y coordinate.
            end_x: End X coordinate.
            end_y: End Y coordinate.

        Returns:
            JSON with wire endpoints and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_wire(p, start_x, start_y, end_x, end_y)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        change_log.record(
            "add_wire",
            {"path": path, "start": [start_x, start_y], "end": [end_x, end_y]},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_label(
        path: str,
        text: str,
        x: float,
        y: float,
        label_type: str = "net_label",
    ) -> str:
        """Add a net label to the schematic.

        Args:
            path: Path to .kicad_sch file.
            text: Label text (net name like VCC, GND, SDA).
            x: X position.
            y: Y position.
            label_type: Type of label - 'net_label', 'global_label', or 'hierarchical_label'.

        Returns:
            JSON with label details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        valid_types = {"net_label", "global_label", "hierarchical_label"}
        if label_type not in valid_types:
            return json.dumps({
                "status": "error",
                "message": f"Invalid label_type: {label_type}. Must be one of: {valid_types}",
            })

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_label(p, text, x, y, label_type)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        change_log.record(
            "add_label",
            {"path": path, "text": text, "x": x, "y": y, "label_type": label_type},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def annotate_schematic(path: str) -> str:
        """Auto-annotate component reference designators in the schematic.

        Args:
            path: Path to .kicad_sch file.

        Returns:
            JSON with annotation results.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        try:
            ops = backend.get_schematic_modify_ops()
            result = ops.annotate(p)
            change_log.record(
                "annotate_schematic", {"path": path},
                file_modified=path,
                backup_path=str(backup) if backup else None,
            )
            return json.dumps({"status": "success", **result}, indent=2)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "info",
                "message": "Auto-annotation requires kicad-cli or KiCad IPC. "
                           "Not available with current backends.",
            })

    @mcp.tool()
    def generate_netlist(path: str, output: str) -> str:
        """Generate a netlist from the schematic.

        Args:
            path: Path to .kicad_sch file.
            output: Output path for the netlist file.

        Returns:
            JSON with netlist generation result.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        out = Path(output)

        try:
            ops = backend.get_schematic_ops()
            result = ops.generate_netlist(p, out)
            change_log.record("generate_netlist", {"path": path, "output": output})
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            # The schematic-ops backend can't generate a netlist (KiCad 9 has no
            # eeschema Python API). Fall back to on-PATH kicad-cli, mirroring how
            # run_erc already does (F6).
            from kicad_mcp.utils.platform_helper import find_kicad_cli
            cli = find_kicad_cli()
            if cli is None:
                return json.dumps({
                    "status": "info",
                    "message": "Netlist generation requires kicad-cli or KiCad IPC; "
                               "kicad-cli was not found on PATH.",
                })
            from kicad_mcp.backends.cli_backend import CLIDRCOps
            res = CLIDRCOps(cli).generate_netlist(p, out)
            if not res.get("generated"):
                return json.dumps({
                    "status": "error",
                    "message": "kicad-cli ran but produced no netlist file.",
                    **res,
                }, indent=2)
            change_log.record("generate_netlist", {"path": path, "output": output})
            return json.dumps({"status": "success", **res}, indent=2)

    @mcp.tool()
    def get_symbol_pin_positions(
        path: str,
        reference: str = "",
        references: list[str] | None = None,
    ) -> str:
        """Get absolute schematic coordinates for each pin of one or more placed symbols.

        This is essential for knowing where to connect wires. It reads the symbol's
        placement (position, rotation, mirror) and its library pin definitions,
        then transforms each pin into absolute schematic coordinates.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of a single symbol (e.g. 'U1', 'R3').
                       Used when querying one component at a time.
            references: List of reference designators for batch queries
                        (e.g. ['R1', 'R2', 'U1']). Preferred over repeated
                        single calls when pin positions for multiple components
                        are needed.

        Returns:
            Single mode: JSON with pin_positions mapping pin numbers to
                         {x, y, unit} coordinates, plus units_placed,
                         unit_positions (per-unit {x, y, rotation}), and
                         common_pin_positions — pins shared by all units of a
                         multi-unit symbol, one entry per placed unit
                         ({unit, x, y}); such pins appear ONLY there, not in
                         pin_positions. Single-unit symbols: units_placed is
                         [1] and common_pin_positions is empty.
            Batch mode:  JSON with batch dict keyed by reference, each value
                         containing the same structure.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        ops = backend.get_schematic_ops()

        if references:
            batch: dict[str, Any] = {}
            for ref in references:
                validate_reference(ref)
                batch[ref] = ops.get_symbol_pin_positions(p, ref)
            change_log.record(
                "get_symbol_pin_positions",
                {"path": path, "references": references},
            )
            return json.dumps({"status": "success", "batch": batch}, indent=2)

        # Single-reference path (original behaviour)
        validate_reference(reference)
        result = ops.get_symbol_pin_positions(p, reference)
        change_log.record(
            "get_symbol_pin_positions",
            {"path": path, "reference": reference},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_no_connect(path: str, x: float, y: float) -> str:
        """Add a no-connect (X) marker to the schematic at the given position.

        No-connect markers indicate that a pin is intentionally left unconnected.
        Place them exactly on the pin endpoint.

        Args:
            path: Path to .kicad_sch file.
            x: X position (should match pin endpoint).
            y: Y position (should match pin endpoint).

        Returns:
            JSON with no-connect position and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_no_connect(p, x, y)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        change_log.record(
            "add_no_connect",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_power_symbol(
        path: str,
        name: str,
        x: float,
        y: float,
        rotation: float = 0.0,
    ) -> str:
        """Add a power symbol (e.g. +3V3, GND, +5V) to the schematic.

        Power symbols represent power nets. Common names: +3V3, +5V, GND, VCC, VDD.
        The lib_id is automatically set to 'power:<name>'.

        Args:
            path: Path to .kicad_sch file.
            name: Power net name (e.g. '+3V3', 'GND', '+5V', 'VCC').
            x: X position.
            y: Y position.
            rotation: Rotation in degrees (e.g. 180 for GND symbols pointing down).

        Returns:
            JSON with power symbol details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_power_symbol(p, name, x, y, rotation)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        change_log.record(
            "add_power_symbol",
            {"path": path, "name": name, "x": x, "y": y, "rotation": rotation},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_components(path: str, components: list[dict[str, Any]]) -> str:
        """Add multiple component symbols in a single call. Prefer over looping add_component.

        Each list entry is a dict with the same fields as add_component:
            lib_id (required, e.g. 'Device:R')
            reference (required, e.g. 'R1')
            value (e.g. '10k')
            x, y (mm, required)
            rotation (degrees, default 0.0)
            mirror ('x' or 'y', optional)
            footprint (e.g. 'Resistor_SMD:R_0402_1005Metric', optional)
            properties (dict[str, str], optional)
            unit (int, default 1 — one batch may place several units of one
                  multi-unit reference, one entry per unit)
            in_bom (bool, default true — symbol appears in the BOM)
            on_board (bool, default true — symbol has a board footprint)
            dnp (bool, default false — do-not-populate flag)
            exclude_from_sim (bool, default false — ignored by the simulator)

        Reads the schematic file once, caches every unique lib_id once, builds
        all symbol blocks in memory, and writes once — collapsing N round-trips
        and N file reads into 1.

        Per-item failures (missing fields, unresolvable lib_id, unit out of
        range, (reference, unit) already placed) do NOT abort the batch —
        successful entries still get placed. Exception: a (reference, unit)
        pair repeated WITHIN the input list refuses the whole batch with the
        file untouched ({"status": "refused"}).

        Returns:
            JSON {"status": "success", "placed": [...], "failed": [...]}.
            placed entries: {reference, lib_id, uuid, position, unit}.
            failed entries: {index, reference, reason}.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        for c in components:
            ref = c.get("reference") if isinstance(c, dict) else None
            if isinstance(ref, str) and ref:
                validate_reference(ref)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_components_bulk(p, components)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Bulk component placement not supported by current backend.",
            })

        refs_summary = [
            c.get("reference", "") for c in components if isinstance(c, dict)
        ][:10]
        change_log.record(
            "add_components",
            {"path": path, "count": len(components), "references": refs_summary},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_power_symbols(path: str, symbols: list[dict[str, Any]]) -> str:
        """Add multiple power symbols in a single call. Prefer over looping add_power_symbol.

        Each list entry is a dict with:
            name (required, e.g. 'VCC', '+3V3', 'GND')
            x, y (mm, required)
            rotation (degrees, default 0.0)

        Reads the schematic once, caches each unique 'power:<name>' lib symbol
        once, scans existing #PWR refs once to determine the next number, and
        writes once. References are auto-incremented sequentially across the
        batch (resuming from the current max in the file).

        Returns:
            JSON {"status": "success", "placed": [...], "failed": [...]}.
            placed entries: {name, reference (#PWRxxx), lib_id, uuid, position}.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_power_symbols_bulk(p, symbols)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Bulk power symbol placement not supported by current backend.",
            })

        names_summary = [
            s.get("name", "") for s in symbols if isinstance(s, dict)
        ][:10]
        change_log.record(
            "add_power_symbols",
            {"path": path, "count": len(symbols), "names": names_summary},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def connect_pins(
        path: str, pins: list[str], net: str, stub_length: float = 2.54,
    ) -> str:
        """Connect multiple pins to one named net in a single call.

        For each pin in `pins` (format: 'REFERENCE.PIN_NUMBER', e.g. 'U1.5'),
        places a short stub wire ending in a net label. All pins receive the
        same net name and are therefore electrically connected. Replaces the
        per-pin add_wire + add_label pattern (~6 calls per net) with one call.

        Args:
            path: Path to .kicad_sch file.
            pins: List of 'REFERENCE.PIN' strings, e.g. ['U1.5', 'R3.2', 'C1.1'].
                  Pin numbers only in v1 (pin names not yet supported).
            net: Net name applied to all pins (validated against KiCad grammar).
            stub_length: Stub wire length in mm. Default 2.54 (one grid unit).
                         Set to 0 to place the label directly at the pin
                         (no stub wire). 0 is always electrically valid and
                         avoids the cardinal-snap heuristic for unusual layouts.

        The schematic must NOT be open in eeschema while this runs — eeschema
        does not auto-reload after file writes, and you'll see stale content.

        Multi-unit symbols: a pin belonging to unit N connects at unit N's
        placed block. A COMMON pin (unit 0, e.g. a dual op-amp's V+ ) gets a
        wire+label at EVERY placed unit — netting fewer than all instances is
        an ERC different_unit_net error. A pin whose owning unit is not placed
        fails with "belongs to unit N, which is not placed".

        Returns:
            JSON {"status": "success", "connected": [...], "failed": [...]}.
            connected entries: {pin, reference, pin_number, x, y,
                                label_uuid, wire_uuid (null if stub_length=0),
                                instances}. x/y/uuids describe the lowest-unit
            placement; common pins with instances > 1 add "placements":
            [{unit, x, y, label_uuid, wire_uuid}, ...] — one per placed unit.
            failed entries: {pin, reason}. If the pin is missing because of a
            known KiCad 9->10 symbol pin rename (e.g. the USB-C shield pin
            S1 -> SH) and the renamed pin exists on the symbol, the failure
            also carries {known_rename, suggested_pin} and a
            {type: "pin_rename_9_to_10"} warning naming the new pin — the
            connection is NOT auto-substituted; re-run with the suggested pin.
            May include "warnings": each stub is collision-checked against every
            other pin; if the preferred direction would short onto a neighbour
            the stub is rerouted, shortened, or (last resort) terminated on the
            pin — reported as {type: "stub_collision", pin, conflict_pins,
            fallback}. A stub is never silently placed onto another pin.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_net_name(net)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.connect_pins_bulk(p, pins, net, stub_length=stub_length)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Bulk pin connection not supported by current backend.",
            })

        change_log.record(
            "connect_pins",
            {"path": path, "net": net, "count": len(pins), "pins": pins[:10],
             "stub_length": stub_length},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_no_connects(path: str, points: list[dict[str, Any]]) -> str:
        """Mark multiple unused pins with no-connect (X) markers in one call.

        Each entry: {"x": float, "y": float}. Place each marker exactly on
        the pin endpoint coordinate (use get_symbol_pin_positions to look
        up coords). Replaces N × add_no_connect calls with 1 — single
        file read/write regardless of marker count.

        Returns:
            JSON {"status": "success", "placed": [...], "failed": [...]}.
            placed entries: {position: {x, y}, uuid}.
            failed entries: {index, reason}.
            Per-item failures don't abort the batch.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_no_connects_bulk(p, points)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Bulk no-connect placement not supported by current backend.",
            })

        points_summary = [
            {"x": pt.get("x"), "y": pt.get("y")}
            for pt in points if isinstance(pt, dict)
        ][:10]
        change_log.record(
            "add_no_connects",
            {"path": path, "count": len(points), "points": points_summary},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def move_components(path: str, moves: list[dict[str, Any]]) -> str:
        """Move multiple schematic components in one call.

        Each entry: {"reference": str, "x": float, "y": float,
                     "rotation": float | None (optional),
                     "unit": int (optional, default 1 — which unit's block
                     moves for a multi-unit symbol)}.
        Property labels (Reference, Value, Footprint, etc.) shift by the
        same delta so they stay aligned with the symbol body.

        Reads/writes the .kicad_sch file once regardless of move count.
        Use this instead of looping move_schematic_component when applying
        a placement plan with multiple repositions.

        Note: moving a component does NOT update wires/labels at the old
        pin positions. After repositioning, re-run connect_pins or
        manually patch wires that were tied to the old coords.

        Returns:
            JSON {"status": "success", "moved": [...], "failed": [...]}.
            moved entries: {reference, position: {x, y}, rotation}.
            failed entries: {index, reference, reason}.
            Per-component failures don't abort the batch.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        for m in moves:
            ref = m.get("reference") if isinstance(m, dict) else None
            if isinstance(ref, str) and ref:
                validate_reference(ref)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.move_components_bulk(p, moves)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Bulk component move not supported by current backend.",
            })

        refs_summary = [
            m.get("reference", "") for m in moves if isinstance(m, dict)
        ][:10]
        change_log.record(
            "move_components",
            {"path": path, "count": len(moves), "references": refs_summary},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    def _remove_schematic_component(
        sch_p: Path, reference: str, unit: int | None = None,
    ) -> dict[str, Any]:
        backup = create_backup(sch_p)
        ops = backend.get_schematic_modify_ops()
        result = ops.remove_component(sch_p, reference, unit=unit)
        change_log.record(
            "remove_component",
            {"path": str(sch_p), "reference": reference, "scope": "schematic",
             "unit": unit},
            file_modified=str(sch_p),
            backup_path=str(backup) if backup else None,
        )
        return result

    def _remove_board_component(pcb_p: Path, reference: str) -> dict[str, Any]:
        """Bridge-first footprint removal with file-backend fallback (#3)."""
        from kicad_mcp.backends.file_backend import FileBoardOps
        from kicad_mcp.backends.plugin_backend import BridgeTemporarilyUnavailableError

        backup = create_backup(pcb_p)
        try:
            result = backend.get_board_modify_ops().remove_component(pcb_p, reference)
        except (BridgeTemporarilyUnavailableError, NotImplementedError):
            result = FileBoardOps().remove_component(pcb_p, reference)
        change_log.record(
            "remove_component",
            {"path": str(pcb_p), "reference": reference, "scope": "pcb"},
            file_modified=str(pcb_p),
            backup_path=str(backup) if backup else None,
        )
        return result

    @mcp.tool()
    def remove_component(
        path: str, reference: str, scope: str = "schematic",
        unit: int | None = None,
    ) -> str:
        """Remove a component from the schematic and/or PCB by reference designator.

        scope="schematic" (default) removes the symbol instance block from the
        .kicad_sch file. This does NOT remove associated wires or labels.
        Power symbols such as #PWR001 or #FLG01 can also be removed.
        For multi-unit symbols, ``unit`` (schematic scope only) removes just
        that unit's block; the default None removes every unit of the
        reference — the whole component.

        scope="pcb" removes the footprint from the .kicad_pcb file (live board
        when the bridge is up, file edit otherwise) and returns the captured
        placement state: footprint lib_id, position, rotation, layer, locked,
        and the pad→net map — everything needed to re-place a replacement
        footprint at the same spot.

        scope="both" removes from both files; pass either file's path and the
        sibling is derived by swapping the extension.

        Args:
            path: Path to .kicad_sch (scope=schematic), .kicad_pcb (scope=pcb),
                  or either (scope=both).
            reference: Reference designator to remove (e.g. 'R1', 'U3', '#PWR031').
            scope: "schematic", "pcb", or "both".

        Returns:
            JSON confirming removal; for pcb/both, includes the captured state.
        """
        if scope not in ("schematic", "pcb", "both"):
            return json.dumps({
                "status": "error",
                "message": f"Invalid scope '{scope}' — use 'schematic', 'pcb', or 'both'.",
            })
        validate_reference(reference)

        if scope == "schematic":
            sch_p = validate_kicad_path(path, ".kicad_sch")
            try:
                result = _remove_schematic_component(sch_p, reference, unit)
            except BoardOpenInKiCadError as exc:
                return _schematic_write_refusal(exc)
            except ValueError as exc:
                return json.dumps({"status": "error", "message": str(exc)})
            return json.dumps({"status": "success", **result}, indent=2)

        if scope == "pcb":
            pcb_p = validate_kicad_path(path, ".kicad_pcb")
            try:
                result = _remove_board_component(pcb_p, reference)
            except BoardOpenInKiCadError as exc:
                # KIR4 NS invariant: never edit the board file behind KiCad.
                return json.dumps({
                    "status": "error",
                    "error_code": "board_open_in_kicad",
                    "message": str(exc),
                }, indent=2)
            except ValueError as exc:
                return json.dumps({"status": "error", "message": str(exc)})
            return json.dumps({"status": "success", **result}, indent=2)

        # scope == "both": derive the sibling file from whichever was given
        base = Path(path)
        sch_p = validate_kicad_path(str(base.with_suffix(".kicad_sch")), ".kicad_sch")
        pcb_p = validate_kicad_path(str(base.with_suffix(".kicad_pcb")), ".kicad_pcb")

        sides: dict[str, dict[str, Any]] = {}
        errors = 0
        try:
            sides["schematic"] = _remove_schematic_component(sch_p, reference)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            sides["schematic"] = {"error": str(exc)}
            errors += 1
        try:
            sides["pcb"] = _remove_board_component(pcb_p, reference)
        except ValueError as exc:
            sides["pcb"] = {"error": str(exc)}
            errors += 1

        status = "success" if errors == 0 else ("partial" if errors == 1 else "error")
        return json.dumps({"status": status, "scope": scope, **sides}, indent=2)

    @mcp.tool()
    def remove_wire(
        path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> str:
        """Remove a wire segment from the schematic identified by its endpoints.

        The wire is matched by comparing start/end coordinates within a small
        tolerance (0.01 mm). Use read_schematic to find exact wire coordinates.

        Args:
            path: Path to .kicad_sch file.
            start_x: Start X coordinate of the wire.
            start_y: Start Y coordinate of the wire.
            end_x: End X coordinate of the wire.
            end_y: End Y coordinate of the wire.

        Returns:
            JSON confirming removal.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.remove_wire(p, start_x, start_y, end_x, end_y)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_wire",
            {"path": path, "start": [start_x, start_y], "end": [end_x, end_y]},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def remove_no_connect(path: str, x: float, y: float) -> str:
        """Remove a no-connect (X) marker from the schematic at the given position.

        The no-connect is matched by comparing its position within a small
        tolerance (0.01 mm). Use read_schematic to find exact no-connect positions.

        Args:
            path: Path to .kicad_sch file.
            x: X position of the no-connect marker.
            y: Y position of the no-connect marker.

        Returns:
            JSON confirming removal.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.remove_no_connect(p, x, y)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_no_connect",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def remove_label(
        path: str, x: float, y: float, text: str | None = None,
    ) -> str:
        """Remove a net label from the schematic at the given position.

        Matches local, global, and hierarchical labels by position within a
        small tolerance (0.01 mm). Pass `text` to disambiguate when several
        labels sit close together. Use read_schematic to find exact label
        positions; a no-match error lists the nearest labels.

        Args:
            path: Path to .kicad_sch file.
            x: X position of the label.
            y: Y position of the label.
            text: Optional label text that must also match.

        Returns:
            JSON confirming removal (includes the removed text and label type).
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.remove_label(p, x, y, text=text)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "remove_label",
            {"path": path, "x": x, "y": y, "text": text},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def set_label_text(
        path: str, x: float, y: float, new_text: str,
        old_text: str | None = None,
    ) -> str:
        """Rename a net label in the schematic at the given position.

        Matches local, global, and hierarchical labels by position within a
        small tolerance (0.01 mm) and replaces the label text in place —
        position, orientation, and styling are untouched. Pass `old_text`
        to disambiguate when several labels sit close together. A no-match
        error lists the nearest labels.

        Args:
            path: Path to .kicad_sch file.
            x: X position of the label.
            y: Y position of the label.
            new_text: Replacement label text (the new net name).
            old_text: Optional current text that must also match.

        Returns:
            JSON with old and new text and the label type.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.set_label_text(p, x, y, new_text, old_text=old_text)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "set_label_text",
            {"path": path, "x": x, "y": y, "new_text": new_text, "old_text": old_text},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def move_schematic_component(
        path: str,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
        unit: int = 1,
    ) -> str:
        """Move a schematic component to a new position.

        Updates the symbol's placement coordinates and shifts all property
        label positions by the same delta so they stay aligned. Optionally
        updates the rotation.

        Note: This is for schematic symbols. Use move_component for PCB footprints.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component to move (e.g. 'R1', 'U3').
            x: New X position in schematic units (mm).
            y: New Y position in schematic units (mm).
            rotation: New rotation in degrees (0, 90, 180, 270). Leave None to keep current.
            unit: Which unit's block to move for a multi-unit symbol
                  (default 1) — moving a multi-unit reference without unit
                  moves unit 1 only.

        Returns:
            JSON with new position and rotation.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.move_component(p, reference, x, y, rotation=rotation, unit=unit)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "move_schematic_component",
            {"path": path, "reference": reference, "x": x, "y": y,
             "rotation": rotation, "unit": unit},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def update_component_property(
        path: str,
        reference: str,
        property_name: str,
        property_value: str,
    ) -> str:
        """Update or add a property on a placed schematic component.

        Can modify Value, Footprint, Datasheet, MPN, or any custom property.
        If the property does not exist on the component, it will be added (hidden by default).
        The symbol attribute flags (in_bom / on_board / dnp /
        exclude_from_sim) are not properties — use set_symbol_attributes
        for those.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component (e.g. 'R1', 'U3').
            property_name: Property name (e.g. 'Value', 'Footprint', 'Datasheet', 'MPN').
            property_value: New value for the property.

        Returns:
            JSON confirming the update.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.update_component_property(p, reference, property_name, property_value)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "update_component_property",
            {"path": path, "reference": reference,
             "property": property_name, "value": property_value},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def set_symbol_attributes(
        path: str,
        reference: str,
        in_bom: bool | None = None,
        on_board: bool | None = None,
        dnp: bool | None = None,
        exclude_from_sim: bool | None = None,
    ) -> str:
        """Set the bare symbol attribute flags on a placed schematic symbol.

        The four flags are bare s-expression tokens KiCad stores on every
        placed symbol — not (property ...) clauses, so
        update_component_property cannot reach them. Every unit block of the
        reference is edited; omitted (None) flags stay as they are. At least
        one flag must be passed.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the component (e.g. 'P5', 'U3').
            in_bom: Symbol appears in the bill of materials.
            on_board: Symbol has a board footprint.
            dnp: Do-not-populate flag.
            exclude_from_sim: Ignored by the simulator.

        Returns:
            JSON with reference, units_updated, and the final effective
            attributes re-read from the edited file.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.set_symbol_attributes(
                p, reference,
                in_bom=in_bom, on_board=on_board,
                dnp=dnp, exclude_from_sim=exclude_from_sim,
            )
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        params: dict[str, Any] = {"path": path, "reference": reference}
        for name, flag in (
            ("in_bom", in_bom), ("on_board", on_board),
            ("dnp", dnp), ("exclude_from_sim", exclude_from_sim),
        ):
            if flag is not None:
                params[name] = flag
        change_log.record(
            "set_symbol_attributes",
            params,
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def compare_schematic_pcb(schematic_path: str, board_path: str) -> str:
        """Compare schematic and PCB to find mismatches.

        Read-only diagnostic that detects reference designator mismatches,
        missing components, footprint differences, and value differences
        between a schematic and its associated PCB.

        Power symbols (references starting with '#') are excluded from
        the comparison since they don't appear in the PCB.

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.

        Returns:
            JSON with comparison results: missing_from_pcb, missing_from_schematic,
            footprint_mismatches, value_mismatches, and matched count.
        """
        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")

        from kicad_mcp.backends.file_backend import FileSchematicOps

        sch_data = FileSchematicOps().read_schematic(sch_p)
        pcb_data = backend.get_board_ops().read_board(pcb_p)

        # Build dicts keyed by reference, filtering out power symbols.
        # Multi-unit components appear once per unit; merge so the footprint
        # from whichever unit carries it is preserved.
        sch_by_ref: dict[str, dict[str, Any]] = {}
        for sym in sch_data.get("symbols", []):
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#"):
                continue
            if sym.get("is_power"):
                continue
            if ref not in sch_by_ref:
                sch_by_ref[ref] = sym
            elif sym.get("footprint") and not sch_by_ref[ref].get("footprint"):
                sch_by_ref[ref]["footprint"] = sym["footprint"]

        pcb_by_ref: dict[str, dict[str, Any]] = {}
        for comp in pcb_data.get("components", []):
            ref = comp.get("reference", "")
            if ref:
                pcb_by_ref[ref] = comp

        all_refs = set(sch_by_ref) | set(pcb_by_ref)
        missing_from_pcb = []
        missing_from_schematic = []
        footprint_mismatches = []
        value_mismatches = []
        matched = 0

        for ref in sorted(all_refs):
            in_sch = ref in sch_by_ref
            in_pcb = ref in pcb_by_ref

            if in_sch and not in_pcb:
                missing_from_pcb.append({
                    "reference": ref,
                    "value": sch_by_ref[ref].get("value", ""),
                    "lib_id": sch_by_ref[ref].get("lib_id", ""),
                })
                continue
            if in_pcb and not in_sch:
                missing_from_schematic.append({
                    "reference": ref,
                    "value": pcb_by_ref[ref].get("value", ""),
                    "footprint": pcb_by_ref[ref].get("footprint", ""),
                })
                continue

            # Both exist — compare
            sch_sym = sch_by_ref[ref]
            pcb_comp = pcb_by_ref[ref]
            mismatch = False

            sch_fp = sch_sym.get("footprint", "")
            pcb_fp = pcb_comp.get("footprint", "")
            if sch_fp and pcb_fp and sch_fp != pcb_fp:
                footprint_mismatches.append({
                    "reference": ref,
                    "schematic_footprint": sch_fp,
                    "pcb_footprint": pcb_fp,
                })
                mismatch = True

            sch_val = sch_sym.get("value", "")
            pcb_val = pcb_comp.get("value", "")
            if sch_val and pcb_val and sch_val != pcb_val:
                value_mismatches.append({
                    "reference": ref,
                    "schematic_value": sch_val,
                    "pcb_value": pcb_val,
                })
                mismatch = True

            if not mismatch:
                matched += 1

        result = {
            "schematic": str(sch_p),
            "board": str(pcb_p),
            "summary": {
                "schematic_components": len(sch_by_ref),
                "pcb_components": len(pcb_by_ref),
                "matched": matched,
                "missing_from_pcb": len(missing_from_pcb),
                "missing_from_schematic": len(missing_from_schematic),
                "footprint_mismatches": len(footprint_mismatches),
                "value_mismatches": len(value_mismatches),
            },
            "missing_from_pcb": missing_from_pcb,
            "missing_from_schematic": missing_from_schematic,
            "footprint_mismatches": footprint_mismatches,
            "value_mismatches": value_mismatches,
        }

        change_log.record(
            "compare_schematic_pcb",
            {"schematic_path": schematic_path, "board_path": board_path},
        )
        return json.dumps({"status": "success", **limit_response(result)}, indent=2)

    @mcp.tool()
    def get_pin_net(path: str, reference: str, pin_number: str) -> str:
        """Get the net name connected to a specific pin of a placed symbol.

        Uses schematic connectivity analysis (wires, labels, power symbols)
        to determine which net a pin belongs to.

        Args:
            path: Path to .kicad_sch file.
            reference: Reference designator of the symbol (e.g. 'R1', 'U1').
            pin_number: Pin number as defined in the symbol library (e.g. '1', '2').

        Returns:
            JSON with reference, pin_number, net_name, and position.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        ops = backend.get_schematic_ops()
        try:
            result = ops.get_pin_net(p, reference, pin_number)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Net connectivity queries not supported by current backend.",
            })
        change_log.record(
            "get_pin_net",
            {"path": path, "reference": reference, "pin_number": pin_number},
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_net_connections(path: str, net_name: str) -> str:
        """Get all connections on a named net in the schematic.

        Returns every pin, label, and wire segment belonging to the given net,
        determined by schematic connectivity analysis.

        Args:
            path: Path to .kicad_sch file.
            net_name: The net name to query (e.g. 'VCC', 'GND', 'Net-(R1-1)').

        Returns:
            JSON with net_name, pins list, labels list, and wires list.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        ops = backend.get_schematic_ops()
        try:
            result = ops.get_net_connections(p, net_name)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Net connectivity queries not supported by current backend.",
            })
        change_log.record(
            "get_net_connections",
            {"path": path, "net_name": net_name},
        )
        return json.dumps({"status": "success", **limit_response(result)}, indent=2)

    @mcp.tool()
    def sync_schematic_to_pcb(
        schematic_path: str, board_path: str,
        apply_footprint_changes: bool = False,
    ) -> str:
        """Synchronize the schematic to the PCB using KiCad's own netlist (KIR4 NS).

        KiCad computes the truth: components (ref/value/footprint) and the
        pad→net map come from `kicad-cli sch export netlist` — never from
        MCP-side connectivity analysis. Board mutations go through the live
        path router (IPC → bridge; loud refusal instead of file writes while
        KiCad is open). Steps: netlist → footprint gate (blocked with ranked
        candidates when unresolvable) → bulk placement (pcbnew loads geometry
        natively — no stubs) → sheet-path/ClusterId metadata + PlacementIntent
        anchors → footprint swaps (opt-in) → values → pad nets → a verify pass
        comparing board pads against the netlist.

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.
            apply_footprint_changes: Swap mismatched PCB footprints to match
                the schematic (default False reports them as warnings only).

        Returns:
            JSON {status, summary, actions, verified, footprint_changes_*,
            warnings}. Error payloads carry error_code: one of
            netlist_generation_failed, netlist_parse_failed, board_read_failed,
            live_path_required, placement_failed. Genuinely-unconnected pads
            stay netless (KiCad's own updater would give them unconnected-*
            nets) — a named representational difference, not a defect.
        """
        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")
        project_dir = pcb_p.parent

        from kicad_mcp.backends.file_backend import FileBoardOps, FileLibraryOps
        from kicad_mcp.models.errors import (
            CapabilityNotSupportedError,
            SafeRefuseError,
        )
        from kicad_mcp.utils.fp_lib_table import get_footprint_library_map
        from kicad_mcp.utils.gates import warn_if_ungated
        from kicad_mcp.utils.netlist_model import (
            NetlistParseError,
            parse_kicad_netlist,
        )
        from kicad_mcp.utils.placement_metrics import read_board_pads

        actions: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        # Step 2 — soft gate (unchanged): nudge, don't block.
        gate_warning = warn_if_ungated(
            sch_p, "validate_schematic_for_pcb", "sync_schematic_to_pcb",
            fix_hint="Run validate_schematic_for_pcb(schematic_path) and resolve blocking issues.",
        )
        if gate_warning is not None:
            warnings.append(gate_warning)

        # Step 3 — KiCad computes the truth (REQ-NS-1).
        try:
            drc_ops = backend.get_drc_ops()
        except CapabilityNotSupportedError as exc:
            return json.dumps({
                "status": "error",
                "error_code": "netlist_generation_failed",
                "message": (
                    f"kicad-cli is unavailable: {exc}. "
                    "Sync requires KiCad's own netlist."
                ),
            }, indent=2)
        netlist_out = pcb_p.parent / f".{pcb_p.stem}.sync_netlist.net"
        try:
            try:
                nl_result = drc_ops.generate_netlist(sch_p, netlist_out)
            except Exception as exc:  # A3: raising backends fail loudly too
                return json.dumps({
                    "status": "error",
                    "error_code": "netlist_generation_failed",
                    "message": (
                        f"kicad-cli netlist generation failed: {exc}. "
                        "Sync requires KiCad's own netlist."
                    ),
                }, indent=2)
            if (not nl_result.get("generated")
                    or nl_result.get("returncode") not in (0, None)):
                return json.dumps({
                    "status": "error",
                    "error_code": "netlist_generation_failed",
                    "message": (
                        "kicad-cli netlist generation failed (returncode "
                        f"{nl_result.get('returncode')}). Fix the schematic "
                        "(run run_erc for details) and retry."
                    ),
                }, indent=2)
            try:
                model = parse_kicad_netlist(netlist_out)
            except NetlistParseError as exc:
                return json.dumps({
                    "status": "error",
                    "error_code": "netlist_parse_failed",
                    "message": str(exc),
                }, indent=2)
        finally:
            netlist_out.unlink(missing_ok=True)

        # Step 4 — footprint gate (REQ-NS-6): every footprint's library AND
        # file must resolve project-aware before any mutation.
        fp_map = get_footprint_library_map(project_dir)
        unresolvable: list[dict[str, Any]] = []
        for comp in model.components:
            fp = comp.footprint
            if not fp:
                warnings.append({
                    "type": "no_footprint",
                    "reference": comp.reference,
                    "message": (
                        f"{comp.reference} has no footprint assigned in "
                        "schematic, cannot place on PCB."
                    ),
                })
                continue
            nick, _, name = fp.partition(":")
            if nick not in fp_map:
                unresolvable.append({
                    "ref": comp.reference, "footprint": fp,
                    "reason": "footprint library not found",
                })
            elif not (Path(fp_map[nick]) / f"{name}.kicad_mod").exists():
                unresolvable.append({
                    "ref": comp.reference, "footprint": fp,
                    "reason": f"footprint '{name}' not found in library '{nick}'",
                })
        if unresolvable:
            lib_ops = FileLibraryOps(project_dir=str(sch_p.parent))
            for entry in unresolvable:
                entry["candidates"] = suggest_footprint_candidates(
                    entry["footprint"], library_ops=lib_ops, limit=5,
                )
            return json.dumps({
                "status": "blocked",
                "reason": "footprint_unresolvable",
                "unresolvable": unresolvable,
                "message": (
                    f"sync refused: {len(unresolvable)} footprint(s) "
                    "unresolvable from the project's libraries. Fix the "
                    "Footprint field or register the library, then retry."
                ),
            }, indent=2)

        # Step 5 — router-served mutations only (REQ-NS-2).
        try:
            ops = backend.get_board_modify_ops()
        except SafeRefuseError as exc:
            return json.dumps({
                "status": "error",
                "error_code": "live_path_required",
                "message": (
                    "KiCad is running but no live path (IPC or bridge) can "
                    "serve board mutations. Sync will not write the board "
                    "file behind KiCad's back. Open the board in the PCB "
                    "editor (bridge) or close KiCad to allow file-mode sync."
                ),
                "detail": str(exc),
            }, indent=2)

        # Step 6 — board read, no silent fallback.
        try:
            components_now = backend.get_board_ops().get_components(pcb_p)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error_code": "board_read_failed",
                "detail": str(exc),
                "message": (
                    "could not read the board through the active backend; "
                    "sync aborted before any mutation."
                ),
            }, indent=2)
        pcb_by_ref: dict[str, dict[str, Any]] = {
            c["reference"]: c for c in components_now if c.get("reference")
        }

        # Step 7 — schematic metadata read: sheet paths, PlacementIntent, and
        # multi-unit footprint-conflict detection for the swap path.
        meta_by_ref: dict[str, tuple[str, str]] = {}
        conflicting_fp_refs: set[str] = set()
        metadata_available = True
        try:
            sch_syms = backend.get_schematic_ops().read_schematic(sch_p).get(
                "symbols", [])
            for sym in sch_syms:
                ref = sym.get("reference", "")
                if not ref or ref.startswith("#") or sym.get("is_power"):
                    continue
                if ref not in meta_by_ref:
                    meta_by_ref[ref] = (
                        sym.get("sheet_path", "") or "",
                        (sym.get("properties") or {}).get(
                            "PlacementIntent", "") or "",
                    )
                # The reader merges multi-unit blocks to one row and records
                # any footprint disagreement between units on that row.
                if sym.get("footprint_conflict"):
                    conflicting_fp_refs.add(ref)
        except Exception as exc:
            metadata_available = False
            warnings.append({
                "type": "metadata_source_unavailable",
                "message": str(exc),
            })

        fp_by_ref = {c.reference: c for c in model.components}
        target = model.pad_net_map()
        placed_refs: set[str] = set()
        swapped_refs: set[str] = set()
        footprint_changes_applied: list[dict[str, Any]] = []
        footprint_changes_skipped: list[dict[str, Any]] = []
        swaps_used_file_path = False

        def _partial_error(code: str, message: str) -> str:
            return json.dumps({
                "status": "error",
                "error_code": code,
                "message": message,
                "partial": {"actions": actions, "warnings": warnings},
            }, indent=2)

        # A1: one backup before the first board mutation.
        create_backup(pcb_p)

        try:
            # Step 8 — placement: one bulk call; pcbnew loads geometry
            # natively inside KiCad (no stubs, loud per-item failures).
            missing = [
                c for c in model.components
                if c.reference not in pcb_by_ref and c.footprint
            ]
            if missing:
                batch: list[dict[str, Any]] = []
                place_x, place_y = 50.0, 50.0
                for comp in missing:
                    batch.append({
                        "reference": comp.reference,
                        "footprint": comp.footprint,
                        "x": place_x, "y": place_y,
                    })
                    place_x += 10.0
                    if place_x > 200.0:
                        place_x = 50.0
                        place_y += 10.0
                positions = {b["reference"]: (b["x"], b["y"]) for b in batch}
                try:
                    bulk = ops.place_components_bulk(pcb_p, batch)
                except SafeRefuseError:
                    raise
                except Exception as exc:
                    return _partial_error("placement_failed", (
                        f"bulk placement failed: {exc}. The board may be "
                        "partially synced; sync is idempotent — placement "
                        "skips existing refs and values/nets re-converge — "
                        "fix the cause and re-run sync."
                    ))
                if bulk.get("status") == "refused":
                    return _partial_error("placement_failed", (
                        f"bulk placement refused: {bulk.get('reason', '')}. "
                        "Fix the cause and re-run sync."
                    ))
                for ref in bulk.get("placed", []):
                    px, py = positions[ref]
                    actions.append({
                        "type": "placed",
                        "reference": ref,
                        "footprint": fp_by_ref[ref].footprint,
                        "position": {"x": px, "y": py},
                    })
                    placed_refs.add(ref)
                for item in bulk.get("failed", []):
                    warnings.append({
                        "type": "place_failed",
                        "reference": item.get("reference", ""),
                        "message": item.get("reason", ""),
                    })

            # Step 9 — metadata + PlacementIntent for freshly placed refs
            # (skipped when the schematic read failed; sync continues).
            if metadata_available:
                for ref in sorted(placed_refs):
                    sheet_path, intent = meta_by_ref.get(ref, ("", ""))
                    cluster_name = ""
                    if intent.startswith("cluster:"):
                        cluster_name = intent.split(":", 1)[1].strip()
                    if sheet_path or cluster_name:
                        try:
                            meta = ops.set_footprint_metadata(
                                pcb_p, ref,
                                sheet_path=sheet_path or None,
                                fields=(
                                    {"ClusterId": cluster_name}
                                    if cluster_name else None
                                ),
                            )
                            actions.append({
                                "type": "metadata_set",
                                "reference": ref,
                                "sheet_path_set": meta.get(
                                    "sheet_path_set", False),
                                "fields_set": meta.get("fields_set", []),
                            })
                            if cluster_name:
                                actions.append({
                                    "type": "cluster_assigned",
                                    "reference": ref,
                                    "cluster": cluster_name,
                                    "source": "PlacementIntent",
                                })
                        except SafeRefuseError:
                            raise
                        except RuntimeError as exc:
                            if "Unknown method" in str(exc):
                                warnings.append({
                                    "type": "bridge_upgrade_required",
                                    "reference": ref,
                                    "message": (
                                        "bridge ≥ 2.6.0-sync required for "
                                        "sheet-path/field metadata; reinstall "
                                        "the bridge and reopen the PCB "
                                        "editor. Metadata skipped; "
                                        "placement/nets unaffected."
                                    ),
                                })
                            else:
                                warnings.append({
                                    "type": "metadata_failed",
                                    "reference": ref,
                                    "message": str(exc),
                                })
                        except Exception as exc:
                            warnings.append({
                                "type": "metadata_failed",
                                "reference": ref,
                                "message": str(exc),
                            })
                    if intent.startswith("edge:"):
                        edge_name = intent.split(":", 1)[1].strip().lower()
                        from kicad_mcp.tools.drc import compute_edge_placement
                        plan = compute_edge_placement(pcb_p, ref, edge_name)
                        if plan["status"] == "success":
                            try:
                                ops.move_component(
                                    pcb_p, ref,
                                    plan["target_x"], plan["target_y"],
                                    plan["target_rotation"],
                                )
                                actions.append({
                                    "type": "anchored_at_edge",
                                    "reference": ref,
                                    "edge": edge_name,
                                    "rotation": plan["target_rotation"],
                                    "source": "PlacementIntent",
                                })
                            except SafeRefuseError:
                                raise
                            except Exception as anchor_exc:
                                warnings.append({
                                    "type": "anchor_failed",
                                    "reference": ref,
                                    "edge": edge_name,
                                    "message": str(anchor_exc),
                                })
                        else:
                            warnings.append({
                                "type": "placement_intent_deferred",
                                "reference": ref,
                                "intent": intent,
                                "message": plan["message"],
                            })

            # Step 10 — extra-on-board + footprint swaps (opt-in).
            for ref in sorted(pcb_by_ref):
                if ref not in fp_by_ref:
                    warnings.append({
                        "type": "extra_in_pcb",
                        "reference": ref,
                        "message": f"{ref} exists in PCB but not in schematic.",
                    })
            for ref in sorted(set(fp_by_ref) & set(pcb_by_ref)):
                sch_fp = fp_by_ref[ref].footprint
                pcb_fp = pcb_by_ref[ref].get("footprint", "")
                if not (sch_fp and pcb_fp and sch_fp != pcb_fp):
                    continue
                if not apply_footprint_changes:
                    warnings.append({
                        "type": "footprint_mismatch",
                        "reference": ref,
                        "schematic_footprint": sch_fp,
                        "pcb_footprint": pcb_fp,
                        "message": (
                            f"{ref} footprint mismatch: schematic={sch_fp}, "
                            f"pcb={pcb_fp}"
                        ),
                    })
                elif ref in conflicting_fp_refs:
                    footprint_changes_skipped.append({
                        "reference": ref,
                        "old_footprint": pcb_fp,
                        "new_footprint": sch_fp,
                        "reason": "multi-unit symbol units disagree on footprint",
                    })
                else:
                    try:
                        swap = _attempt_footprint_swap(
                            pcb_p, ref, sch_fp, board_ops=ops,
                        )
                    except SafeRefuseError:
                        raise
                    except Exception as exc:
                        swap = {"applied": False, "reason": f"swap failed: {exc}"}
                    if swap["applied"]:
                        if swap.get("via") == "file":
                            swaps_used_file_path = True
                        footprint_changes_applied.append(swap["record"])
                        actions.append({
                            "type": "footprint_swapped",
                            **swap["record"],
                        })
                        swapped_refs.add(ref)
                    else:
                        footprint_changes_skipped.append({
                            "reference": ref,
                            "old_footprint": pcb_fp,
                            "new_footprint": sch_fp,
                            "reason": swap["reason"],
                        })
                        # SW-1 C4: a failed rollback left the part deleted —
                        # loud, structured warning. A successful rollback
                        # stays a plain skip (the part is intact).
                        if swap.get("restored") is False:
                            warnings.append({
                                "type": "swap_rollback_failed",
                                "reference": ref,
                                "message": swap["reason"],
                            })
            if swaps_used_file_path:
                warnings.append({
                    "type": "board_reload_required",
                    "message": (
                        "Footprint swaps edited the board file directly "
                        "(KiCad closed); reload the board in pcbnew before "
                        "further live-board operations."
                    ),
                })

            # Step 11 — values, AFTER swaps: netlist value is the truth;
            # placed/swapped refs get it unconditionally (fresh footprints
            # carry library-default Values).
            for comp in model.components:
                ref, value = comp.reference, comp.value
                if not value:
                    continue
                if ref in placed_refs or ref in swapped_refs:
                    old_value = ""
                elif ref in pcb_by_ref:
                    old_value = pcb_by_ref[ref].get("value", "")
                    if old_value == value:
                        continue
                else:
                    continue
                try:
                    ops.set_footprint_value(pcb_p, ref, value)
                    actions.append({
                        "type": "value_updated",
                        "reference": ref,
                        "old_value": old_value,
                        "new_value": value,
                    })
                except SafeRefuseError:
                    raise
                except Exception as exc:
                    warnings.append({
                        "type": "value_update_failed",
                        "reference": ref,
                        "message": str(exc),
                    })

            # Step 12 — pad nets from KiCad's netlist only (REQ-NS-1).
            board_pads = {
                (p["ref"], p["pad"]): p["net_name"]
                for p in read_board_pads(pcb_p.read_text(encoding="utf-8"))
            }
            for (ref, pad), net in sorted(target.items()):
                if board_pads.get((ref, pad)) == net:
                    continue
                try:
                    ops.assign_net(pcb_p, ref, pad, net)
                    actions.append({
                        "type": "net_assigned",
                        "reference": ref,
                        "pad": pad,
                        "net": net,
                    })
                except SafeRefuseError:
                    raise
                except Exception as exc:
                    warnings.append({
                        "type": "net_assign_failed",
                        "reference": ref,
                        "pad": pad,
                        "net": net,
                        "message": str(exc),
                    })
        except SafeRefuseError as exc:
            # A2: the router can refuse at call-time too — same loud payload.
            return json.dumps({
                "status": "error",
                "error_code": "live_path_required",
                "message": (
                    "KiCad is running but no live path (IPC or bridge) can "
                    "serve board mutations. Sync will not write the board "
                    "file behind KiCad's back. Open the board in the PCB "
                    "editor (bridge) or close KiCad to allow file-mode sync."
                ),
                "detail": str(exc),
                "partial": {"actions": actions, "warnings": warnings},
            }, indent=2)

        # Step 13 — verify: the board's pads against KiCad's netlist.
        verified: dict[str, Any]
        try:
            pads_after = {
                (p["ref"], p["pad"]): p["net_name"]
                for p in read_board_pads(pcb_p.read_text(encoding="utf-8"))
            }
            mismatches: list[dict[str, Any]] = []
            matched = 0
            for (ref, pad), net in sorted(target.items()):
                actual = pads_after.get((ref, pad))
                if actual == net:
                    matched += 1
                else:
                    mismatches.append({
                        "reference": ref, "pad": pad,
                        "expected": net, "actual": actual,
                    })
            verified = {
                "pads_expected": len(target),
                "pads_matched": matched,
                "mismatches": mismatches[:25],
                "mismatches_total": len(mismatches),
            }
        except Exception as exc:
            verified = {
                "error": str(exc),
                "message": "verification read failed; treat sync as UNVERIFIED",
            }

        summary = {
            "components_placed": sum(1 for a in actions if a["type"] == "placed"),
            "values_updated": sum(1 for a in actions if a["type"] == "value_updated"),
            "nets_assigned": sum(1 for a in actions if a["type"] == "net_assigned"),
            "metadata_set": sum(1 for a in actions if a["type"] == "metadata_set"),
            "footprint_changes_applied": len(footprint_changes_applied),
            "footprint_changes_skipped": len(footprint_changes_skipped),
            "warnings": len(warnings),
        }

        change_log.record(
            "sync_schematic_to_pcb",
            {
                "schematic_path": schematic_path,
                "board_path": board_path,
                "apply_footprint_changes": apply_footprint_changes,
            },
            file_modified=board_path,
        )
        return json.dumps({
            "status": "success",
            **limit_response({
                "summary": summary,
                "actions": actions,
                "verified": verified,
                "footprint_changes_applied": footprint_changes_applied,
                "footprint_changes_skipped": footprint_changes_skipped,
                "warnings": warnings,
            }),
        }, indent=2)

    @mcp.tool()
    def add_junction(path: str, x: float, y: float) -> str:
        """Add a junction dot to the schematic at the given position.

        Junctions indicate that crossing wires are electrically connected.
        Place them at wire intersection points.

        Args:
            path: Path to .kicad_sch file.
            x: X position (at wire intersection).
            y: Y position (at wire intersection).

        Returns:
            JSON with junction position and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.add_junction(p, x, y)
        except BoardOpenInKiCadError as exc:
            return _schematic_write_refusal(exc)
        change_log.record(
            "add_junction",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)
