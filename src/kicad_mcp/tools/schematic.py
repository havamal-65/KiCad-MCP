"""Schematic tools - 22 tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
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


def _format_validator_refusal_message(sf: dict) -> str:
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


def _attempt_footprint_swap(pcb_p: Path, ref: str, new_fp: str, board_ops=None) -> dict:
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
    mutate_ops.place_component(
        pcb_p, ref, new_fp,
        removed["position"]["x"], removed["position"]["y"],
        layer=removed.get("layer") or "F.Cu",
        rotation=removed.get("rotation", 0.0),
    )

    unmatched_pads: list[dict[str, str]] = []
    for pad_name, net in sorted(old_pad_nets.items()):
        if new_pad_names and pad_name not in new_pad_names:
            unmatched_pads.append({"pad": pad_name, "net": net})
            continue
        try:
            mutate_ops.assign_net(pcb_p, ref, pad_name, net)
        except ValueError:
            unmatched_pads.append({"pad": pad_name, "net": net})

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


def _inject_footprint_sheet_path(pcb_path: Path, reference: str, sheet_path: str) -> None:
    """Add a ``(path "<sheet_path>")`` clause to a placed footprint block.

    The clause is inserted right after the footprint's ``(uuid "...")`` line
    (matching KiCad's native sync output position). Idempotent — if the
    footprint already has a (path ...) clause, it is overwritten. Phase 6.3.3.
    """
    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

    content = pcb_path.read_text(encoding="utf-8")
    located = find_footprint_block_by_reference(content, reference)
    if located is None:
        raise ValueError(f"footprint {reference} not found in {pcb_path}")
    start, end = located
    block = content[start : end + 1]

    import re as _re
    if _re.search(r'\(path\s+"[^"]*"\)', block):
        new_block = _re.sub(
            r'\(path\s+"[^"]*"\)',
            f'(path "{sheet_path}")',
            block,
            count=1,
        )
    else:
        # Insert after the (uuid "...") line. Match KiCad's tabbed indentation.
        new_block = _re.sub(
            r'(\(uuid\s+"[^"]+"\)\n)',
            rf'\1\t\t(path "{sheet_path}")\n',
            block,
            count=1,
        )
        if new_block == block:
            raise ValueError(
                f"could not find (uuid ...) anchor in footprint {reference}"
            )

    pcb_path.write_text(content[:start] + new_block + content[end + 1:], encoding="utf-8")


def _inject_footprint_property(
    pcb_path: Path, reference: str, prop_name: str, prop_value: str,
) -> None:
    """Add (or update) a ``(property NAME VALUE ...)`` clause on a placed footprint.

    Idempotent — if the property already exists with a different value, it's
    overwritten. Used by sync to attach PlacementIntent-derived metadata
    (e.g. ClusterId) onto placed footprints so downstream tools like
    auto_place can read it. Phase 6.3.2 + 6.3.3.
    """
    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

    content = pcb_path.read_text(encoding="utf-8")
    located = find_footprint_block_by_reference(content, reference)
    if located is None:
        raise ValueError(f"footprint {reference} not found in {pcb_path}")
    start, end = located
    block = content[start : end + 1]

    import re as _re
    prop_pattern = _re.compile(
        rf'\(property\s+"{_re.escape(prop_name)}"\s+"[^"]*"[^)]*\)'
    )
    if prop_pattern.search(block):
        new_block = prop_pattern.sub(
            f'(property "{prop_name}" "{prop_value}")',
            block,
            count=1,
        )
    else:
        # Insert after the (uuid "...") line. Match KiCad's tabbed indentation.
        new_block = _re.sub(
            r'(\(uuid\s+"[^"]+"\)\n)',
            rf'\1\t\t(property "{prop_name}" "{prop_value}")\n',
            block,
            count=1,
        )
        if new_block == block:
            raise ValueError(
                f"could not find (uuid ...) anchor in footprint {reference}"
            )

    pcb_path.write_text(content[:start] + new_block + content[end + 1:], encoding="utf-8")


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

        Returns:
            JSON with placed component details and UUID.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        result = ops.add_component(
            p, lib_id, reference, value, x, y,
            rotation=rotation, mirror=mirror,
            footprint=footprint, properties=properties,
        )
        change_log.record(
            "add_component",
            {"path": path, "lib_id": lib_id, "reference": reference, "value": value},
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
        result = ops.add_wire(p, start_x, start_y, end_x, end_y)
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
        result = ops.add_label(p, text, x, y, label_type)
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
            return json.dumps({
                "status": "info",
                "message": "Netlist generation requires kicad-cli or KiCad IPC.",
            })

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
            Single mode: JSON with pin_positions mapping pin numbers to {x, y} coordinates.
            Batch mode:  JSON with batch dict keyed by reference, each value containing
                         the same pin_positions structure.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        ops = backend.get_schematic_ops()

        if references:
            batch: dict = {}
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
        result = ops.add_no_connect(p, x, y)
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
        result = ops.add_power_symbol(p, name, x, y, rotation)
        change_log.record(
            "add_power_symbol",
            {"path": path, "name": name, "x": x, "y": y, "rotation": rotation},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def add_components(path: str, components: list[dict]) -> str:
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

        Reads the schematic file once, caches every unique lib_id once, builds
        all symbol blocks in memory, and writes once — collapsing N round-trips
        and N file reads into 1.

        Per-item failures (missing fields, unresolvable lib_id) do NOT abort the
        batch — successful entries still get placed.

        Returns:
            JSON {"status": "success", "placed": [...], "failed": [...]}.
            placed entries: {reference, lib_id, uuid, position}.
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
    def add_power_symbols(path: str, symbols: list[dict]) -> str:
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

        Returns:
            JSON {"status": "success", "connected": [...], "failed": [...]}.
            connected entries: {pin, reference, pin_number, x, y,
                                label_uuid, wire_uuid (null if stub_length=0)}.
            failed entries: {pin, reason}.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_net_name(net)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.connect_pins_bulk(p, pins, net, stub_length=stub_length)
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
    def add_no_connects(path: str, points: list[dict]) -> str:
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
    def move_components(path: str, moves: list[dict]) -> str:
        """Move multiple schematic components in one call.

        Each entry: {"reference": str, "x": float, "y": float,
                     "rotation": float | None (optional)}.
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

    def _remove_schematic_component(sch_p: Path, reference: str) -> dict:
        backup = create_backup(sch_p)
        ops = backend.get_schematic_modify_ops()
        result = ops.remove_component(sch_p, reference)
        change_log.record(
            "remove_component",
            {"path": str(sch_p), "reference": reference, "scope": "schematic"},
            file_modified=str(sch_p),
            backup_path=str(backup) if backup else None,
        )
        return result

    def _remove_board_component(pcb_p: Path, reference: str) -> dict:
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
    def remove_component(path: str, reference: str, scope: str = "schematic") -> str:
        """Remove a component from the schematic and/or PCB by reference designator.

        scope="schematic" (default) removes the symbol instance block from the
        .kicad_sch file. This does NOT remove associated wires or labels.
        Power symbols such as #PWR001 or #FLG01 can also be removed.

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
                result = _remove_schematic_component(sch_p, reference)
            except ValueError as exc:
                return json.dumps({"status": "error", "message": str(exc)})
            return json.dumps({"status": "success", **result}, indent=2)

        if scope == "pcb":
            pcb_p = validate_kicad_path(path, ".kicad_pcb")
            try:
                result = _remove_board_component(pcb_p, reference)
            except ValueError as exc:
                return json.dumps({"status": "error", "message": str(exc)})
            return json.dumps({"status": "success", **result}, indent=2)

        # scope == "both": derive the sibling file from whichever was given
        base = Path(path)
        sch_p = validate_kicad_path(str(base.with_suffix(".kicad_sch")), ".kicad_sch")
        pcb_p = validate_kicad_path(str(base.with_suffix(".kicad_pcb")), ".kicad_pcb")

        sides: dict[str, dict] = {}
        errors = 0
        try:
            sides["schematic"] = _remove_schematic_component(sch_p, reference)
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

        Returns:
            JSON with new position and rotation.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        validate_reference(reference)

        backup = create_backup(p)
        ops = backend.get_schematic_modify_ops()
        try:
            result = ops.move_component(p, reference, x, y, rotation=rotation)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        change_log.record(
            "move_schematic_component",
            {"path": path, "reference": reference, "x": x, "y": y, "rotation": rotation},
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
        sch_by_ref: dict[str, dict] = {}
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

        pcb_by_ref: dict[str, dict] = {}
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
        """Synchronize schematic components to the PCB board.

        Reads the schematic and PCB, compares them, and applies safe
        automatic changes:
        - Components missing from PCB are placed at auto-positioned locations.
        - Value mismatches are updated on the PCB side.
        - Pin-to-net assignments are propagated from schematic connectivity to PCB pads.
        - Extra PCB components are reported as warnings (manual review).
        - Footprint mismatches: warned by default; with
          apply_footprint_changes=True the PCB footprint is swapped for the
          schematic's one, preserving position, rotation, layer, and pad
          nets (matched by pad name). Ambiguous cases — locked footprints,
          unresolvable new footprints, fully incompatible pad names,
          multi-unit symbols whose units disagree on footprint — are
          skipped with a reason, never guessed. Swaps edit the board file
          directly: if the board is open in pcbnew, reload it afterwards.

        Args:
            schematic_path: Path to .kicad_sch file.
            board_path: Path to .kicad_pcb file.
            apply_footprint_changes: Swap mismatched PCB footprints to match
                the schematic (default False reports them as warnings only).

        Returns:
            JSON with summary of actions taken, footprint changes
            applied/skipped, and warnings.
        """
        sch_p = validate_kicad_path(schematic_path, ".kicad_sch")
        pcb_p = validate_kicad_path(board_path, ".kicad_pcb")

        # §6.2 precondition: refuse before any PCB work when a symbol's Footprint
        # is unresolvable or its pin set isn't a subset of the footprint's pads —
        # otherwise sync silently produces a board with unassigned pads.
        from kicad_mcp.tools.drc import run_validate_symbol_footprint_pairs
        sf = run_validate_symbol_footprint_pairs(sch_p)
        if not sf["passed"]:
            # §6.8: enrich each unresolvable footprint with ranked replacement
            # candidates so recovery is one call, not a search loop. Purely
            # additive — only the new `candidates` key is added (REQ-WIRE-003).
            unresolvable = sf["unresolvable"]
            if unresolvable:
                from kicad_mcp.backends.file_backend import FileLibraryOps
                lib_ops = FileLibraryOps(project_dir=str(sch_p.parent))
                for entry in unresolvable:
                    entry["candidates"] = suggest_footprint_candidates(
                        entry["footprint"], library_ops=lib_ops, limit=5,
                    )
            msg = _format_validator_refusal_message(sf)
            if any(e.get("candidates") for e in unresolvable):
                msg += ("\nRanked replacement footprints are attached per "
                        "unresolvable symbol (`candidates`).")
            return json.dumps({
                "status": "blocked",
                "reason": "symbol_footprint_validator_failed",
                "mismatches": sf["mismatches"],
                "unresolvable": unresolvable,
                "message": msg,
            }, indent=2)

        from kicad_mcp.backends.file_backend import FileBoardOps, FileSchematicOps

        # Try the configured backend first; fall back to file ops if IPC times out
        # (common when KiCad is running but the board/schematic isn't open in the editor).
        try:
            sch_ops = backend.get_schematic_ops()
            sch_data = sch_ops.read_schematic(sch_p)
        except Exception:
            sch_ops = FileSchematicOps()
            sch_data = sch_ops.read_schematic(sch_p)

        try:
            pcb_ops = backend.get_board_ops()
            pcb_data = pcb_ops.read_board(pcb_p)
        except Exception:
            pcb_ops = FileBoardOps()
            pcb_data = pcb_ops.read_board(pcb_p)

        # Build dicts keyed by reference, filtering out power symbols.
        # Multi-unit components appear once per unit; merge so the footprint
        # from whichever unit carries it is preserved.
        sch_by_ref: dict[str, dict] = {}
        conflicting_fp_refs: set[str] = set()
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
            elif (sym.get("footprint") and sch_by_ref[ref].get("footprint")
                  and sym["footprint"] != sch_by_ref[ref]["footprint"]):
                # Multi-unit symbol whose units disagree on footprint —
                # ambiguous for the swap path, skip-with-reason there.
                conflicting_fp_refs.add(ref)

        pcb_by_ref: dict[str, dict] = {}
        for comp in pcb_data.get("components", []):
            ref = comp.get("reference", "")
            if ref:
                pcb_by_ref[ref] = comp

        actions: list[dict] = []
        warnings: list[dict] = []

        # §6.3 soft gate: nudge (don't block — sync is iterative) when the
        # schematic hasn't passed validate_schematic_for_pcb against its current
        # content. The §6.2 footprint check above is the hard gate; this surfaces
        # the broader ERC/connectivity validation the user may have skipped.
        from kicad_mcp.utils.gates import warn_if_ungated
        gate_warning = warn_if_ungated(
            sch_p, "validate_schematic_for_pcb", "sync_schematic_to_pcb",
            fix_hint="Run validate_schematic_for_pcb(schematic_path) and resolve blocking issues.",
        )
        if gate_warning is not None:
            warnings.append(gate_warning)

        # Select board modify ops based on whether the earlier read succeeded via the
        # plugin backend.  If the read had to fall back to FileBoardOps (bridge down or
        # board not open in KiCad), use FileBoardOps for mutations too — that is safe
        # when the PCB was just created and has not been opened in the PCB editor yet.
        # This avoids the previous bug where get_board_modify_ops() always succeeded
        # (it returns an ops object without probing the bridge), then every individual
        # place_component / assign_net call failed with [WinError 10061] and was
        # silently turned into a warning, producing status:"success" with 0 placements.
        if isinstance(pcb_ops, FileBoardOps):
            board_modify_ops = FileBoardOps()
        else:
            try:
                board_modify_ops = backend.get_board_modify_ops()
            except Exception:
                board_modify_ops = FileBoardOps()

        # Auto-position grid for new components
        place_x, place_y = 50.0, 50.0
        place_step = 10.0
        placed_refs: set[str] = set()
        # True if any applied footprint swap edited the .kicad_pcb file directly
        # (file backend) rather than the live bridge board — drives the
        # board_reload_required warning (#2/B3).
        swaps_used_file_path = False

        for ref in sorted(sch_by_ref):
            if ref not in pcb_by_ref:
                # Missing from PCB → place it
                sym = sch_by_ref[ref]
                footprint = sym.get("footprint", "")
                if not footprint:
                    warnings.append({
                        "type": "no_footprint",
                        "reference": ref,
                        "message": f"{ref} has no footprint assigned in schematic, cannot place on PCB.",
                    })
                    continue

                if board_modify_ops is not None:
                    backup = create_backup(pcb_p)
                    try:
                        result = board_modify_ops.place_component(
                            pcb_p, ref, footprint, place_x, place_y,
                        )
                        actions.append({
                            "type": "placed",
                            "reference": ref,
                            "footprint": footprint,
                            "position": {"x": place_x, "y": place_y},
                        })
                        placed_refs.add(ref)
                        place_x += place_step
                        if place_x > 200.0:
                            place_x = 50.0
                            place_y += place_step

                        # Phase 6.3.3 — propagate the schematic sheet path
                        # onto the placed footprint so auto_place can cluster
                        # components by their schematic hierarchy.
                        sheet_path = sym.get("sheet_path", "")
                        if sheet_path:
                            try:
                                _inject_footprint_sheet_path(pcb_p, ref, sheet_path)
                            except Exception as inj_exc:
                                warnings.append({
                                    "type": "sheet_path_inject_failed",
                                    "reference": ref,
                                    "sheet_path": sheet_path,
                                    "message": str(inj_exc),
                                })

                        # PlacementIntent (Phase 6.3.2): schematic-driven anchor
                        # at a named board edge. Requires Edge.Cuts to exist —
                        # surface as a deferred action otherwise.
                        intent = (sym.get("properties") or {}).get("PlacementIntent", "")
                        if intent.startswith("cluster:"):
                            # Phase 6.3.2 + 6.3.3 — cluster:NAME overrides the
                            # natural sheet_path clustering. Inject a ClusterId
                            # property; auto_place reads it as the grouping key.
                            cluster_name = intent.split(":", 1)[1].strip()
                            try:
                                _inject_footprint_property(
                                    pcb_p, ref, "ClusterId", cluster_name,
                                )
                                actions.append({
                                    "type": "cluster_assigned",
                                    "reference": ref,
                                    "cluster": cluster_name,
                                    "source": "PlacementIntent",
                                })
                            except Exception as inj_exc:
                                warnings.append({
                                    "type": "cluster_inject_failed",
                                    "reference": ref,
                                    "cluster": cluster_name,
                                    "message": str(inj_exc),
                                })
                        if intent.startswith("edge:"):
                            edge_name = intent.split(":", 1)[1].strip().lower()
                            from kicad_mcp.tools.drc import compute_edge_placement
                            plan = compute_edge_placement(pcb_p, ref, edge_name)
                            if plan["status"] == "success":
                                try:
                                    board_modify_ops.move_component(
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
                                except Exception as anchor_exc:
                                    warnings.append({
                                        "type": "anchor_failed",
                                        "reference": ref,
                                        "edge": edge_name,
                                        "message": str(anchor_exc),
                                    })
                            else:
                                # Deferred — typically no Edge.Cuts outline yet
                                warnings.append({
                                    "type": "placement_intent_deferred",
                                    "reference": ref,
                                    "intent": intent,
                                    "message": plan["message"],
                                })
                    except Exception as exc:
                        warnings.append({
                            "type": "place_failed",
                            "reference": ref,
                            "message": str(exc),
                        })
                else:
                    warnings.append({
                        "type": "missing_from_pcb",
                        "reference": ref,
                        "footprint": footprint,
                        "message": f"{ref} missing from PCB (board modify not available).",
                    })

        for ref in sorted(pcb_by_ref):
            if ref not in sch_by_ref:
                warnings.append({
                    "type": "extra_in_pcb",
                    "reference": ref,
                    "message": f"{ref} exists in PCB but not in schematic.",
                })

        # Check mismatches for pre-existing components, and update values for all
        # (including newly placed, which start with the footprint name as Value).
        footprint_changes_applied: list[dict] = []
        footprint_changes_skipped: list[dict] = []
        for ref in sorted(set(sch_by_ref) & (set(pcb_by_ref) | placed_refs)):
            sch_sym = sch_by_ref[ref]
            pcb_comp = pcb_by_ref.get(ref)

            sch_fp = sch_sym.get("footprint", "")
            pcb_fp = pcb_comp.get("footprint", "") if pcb_comp else ""
            if sch_fp and pcb_fp and sch_fp != pcb_fp:
                if not apply_footprint_changes:
                    warnings.append({
                        "type": "footprint_mismatch",
                        "reference": ref,
                        "schematic_footprint": sch_fp,
                        "pcb_footprint": pcb_fp,
                        "message": f"{ref} footprint mismatch: schematic={sch_fp}, pcb={pcb_fp}",
                    })
                elif ref in conflicting_fp_refs:
                    footprint_changes_skipped.append({
                        "reference": ref,
                        "old_footprint": pcb_fp,
                        "new_footprint": sch_fp,
                        "reason": "multi-unit symbol units disagree on footprint",
                    })
                else:
                    create_backup(pcb_p)
                    try:
                        swap = _attempt_footprint_swap(
                            pcb_p, ref, sch_fp, board_ops=board_modify_ops,
                        )
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
                        # The freshly embedded footprint carries the .kicad_mod's
                        # own Value text — blank the stale read so the value-update
                        # pass below rewrites it to the schematic's value.
                        if pcb_comp is not None:
                            pcb_comp["value"] = ""
                    else:
                        footprint_changes_skipped.append({
                            "reference": ref,
                            "old_footprint": pcb_fp,
                            "new_footprint": sch_fp,
                            "reason": swap["reason"],
                        })

            sch_val = sch_sym.get("value", "")
            pcb_val = pcb_comp.get("value", "") if pcb_comp else ""
            if sch_val and sch_val != pcb_val:
                # Update the PCB footprint Value through board_modify_ops — the
                # live bridge when one is open (so the value lands on the
                # in-memory board, not just the file behind the bridge's back,
                # which is the #7 revert root cause), else the file backend.
                if board_modify_ops is not None:
                    try:
                        board_modify_ops.set_footprint_value(pcb_p, ref, sch_val)
                        actions.append({
                            "type": "value_updated",
                            "reference": ref,
                            "old_value": pcb_val,
                            "new_value": sch_val,
                        })
                    except ValueError as exc:
                        warnings.append({
                            "type": "value_mismatch",
                            "reference": ref,
                            "message": f"Could not update Value for {ref}: {exc}",
                        })
                    except Exception as exc:
                        warnings.append({
                            "type": "value_update_failed",
                            "reference": ref,
                            "message": str(exc),
                        })
                else:
                    warnings.append({
                        "type": "value_mismatch",
                        "reference": ref,
                        "schematic_value": sch_val,
                        "pcb_value": pcb_val,
                        "message": f"{ref} value mismatch (board modify not available).",
                    })

        # Propagate schematic pin nets to PCB pads.
        # Use _build_connectivity to resolve all nets in one pass when available
        # (avoids re-parsing the schematic file O(N×M) times via per-pin get_pin_net).
        if board_modify_ops is not None:
            if hasattr(sch_ops, "_build_connectivity"):
                try:
                    connectivity = sch_ops._build_connectivity(sch_p)
                except Exception as conn_exc:
                    connectivity = {}
                    warnings.append({
                        "type": "net_sync_failed",
                        "message": f"Could not build schematic connectivity: {conn_exc}",
                    })
                seen_keys: set[tuple[str, str, str]] = set()
                net_sync_aborted = False
                for net_name, pins in connectivity.items():
                    if net_sync_aborted:
                        break
                    if not net_name or net_name.strip().lower() == "none":
                        continue
                    for pin in pins:
                        ref = pin.get("reference", "")
                        pad = str(pin.get("pin_number", ""))
                        if not ref or not pad or ref not in sch_by_ref:
                            continue
                        key = (ref, pad, net_name)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        try:
                            board_modify_ops.assign_net(pcb_p, ref, pad, net_name)
                            actions.append({
                                "type": "net_assigned",
                                "reference": ref,
                                "pad": pad,
                                "net": net_name,
                            })
                        except NotImplementedError:
                            warnings.append({
                                "type": "net_sync_not_supported",
                                "message": "Board backend does not support assigning nets to pads.",
                            })
                            net_sync_aborted = True
                            break
                        except Exception as exc:
                            warnings.append({
                                "type": "net_assign_failed",
                                "reference": ref,
                                "pad": pad,
                                "net": net_name,
                                "message": str(exc),
                            })
            else:
                # IPC backend: fall back to per-pin queries
                net_assignments: list[dict[str, str]] = []
                net_queries_supported = True

                for ref in sorted(sch_by_ref):
                    if ref not in pcb_by_ref and ref not in placed_refs:
                        continue
                    try:
                        pin_info = sch_ops.get_symbol_pin_positions(sch_p, ref)
                    except NotImplementedError:
                        net_queries_supported = False
                        break
                    except Exception:
                        continue
                    if not isinstance(pin_info, dict):
                        continue
                    pin_positions = pin_info.get("pin_positions", {})
                    if not isinstance(pin_positions, dict):
                        continue
                    for pin_number in sorted(pin_positions, key=lambda p: str(p)):
                        pin_number_str = str(pin_number)
                        try:
                            pin_net = sch_ops.get_pin_net(sch_p, ref, pin_number_str)
                        except NotImplementedError:
                            net_queries_supported = False
                            break
                        except Exception:
                            continue
                        if not isinstance(pin_net, dict):
                            continue
                        net_name = pin_net.get("net_name", "")
                        if not isinstance(net_name, str) or not net_name:
                            continue
                        if net_name.strip().lower() == "none":
                            continue
                        net_assignments.append({
                            "reference": ref,
                            "pad": pin_number_str,
                            "net": net_name,
                        })
                    if not net_queries_supported:
                        break

                if not net_queries_supported:
                    warnings.append({
                        "type": "net_sync_not_supported",
                        "message": "Schematic backend does not support pin/net connectivity queries.",
                    })
                else:
                    seen_assignments: set[tuple[str, str, str]] = set()
                    for assignment in net_assignments:
                        key = (assignment["reference"], assignment["pad"], assignment["net"])
                        if key in seen_assignments:
                            continue
                        seen_assignments.add(key)
                        try:
                            board_modify_ops.assign_net(
                                pcb_p,
                                assignment["reference"],
                                assignment["pad"],
                                assignment["net"],
                            )
                            actions.append({"type": "net_assigned", **assignment})
                        except NotImplementedError:
                            warnings.append({
                                "type": "net_sync_not_supported",
                                "message": "Board backend does not support assigning nets to pads.",
                            })
                            break
                        except Exception as exc:
                            warnings.append({
                                "type": "net_assign_failed",
                                "reference": assignment["reference"],
                                "pad": assignment["pad"],
                                "net": assignment["net"],
                                "message": str(exc),
                            })
        else:
            if any(ref in pcb_by_ref or ref in placed_refs for ref in sch_by_ref):
                warnings.append({
                    "type": "net_sync_unavailable",
                    "message": "Board modify backend not available; schematic nets were not synced to PCB pads.",
                })

        # Swaps normally route through the live bridge (B1), so the in-memory
        # board already matches disk and no reload is needed. Warn only when a
        # swap actually fell back to the file path while a live pcbnew board is
        # open (e.g. the bridge dropped mid-sync) — that is the only case where
        # the in-memory board now diverges from disk (#2/B3).
        if (footprint_changes_applied and swaps_used_file_path
                and not isinstance(pcb_ops, FileBoardOps)):
            warnings.append({
                "type": "board_reload_required",
                "message": (
                    "Footprint swaps fell back to editing the board file directly; "
                    "reload the board in pcbnew (reload_board) before further "
                    "live-board operations."
                ),
            })

        summary = {
            "components_placed": sum(1 for a in actions if a["type"] == "placed"),
            "values_updated": sum(1 for a in actions if a["type"] == "value_updated"),
            "nets_assigned": sum(1 for a in actions if a["type"] == "net_assigned"),
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
        result = ops.add_junction(p, x, y)
        change_log.record(
            "add_junction",
            {"path": path, "x": x, "y": y},
            file_modified=path,
            backup_path=str(backup) if backup else None,
        )
        return json.dumps({"status": "success", **result}, indent=2)
