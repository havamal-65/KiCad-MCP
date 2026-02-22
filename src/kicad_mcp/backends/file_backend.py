"""Pure Python file-parsing backend - always available, no KiCad installation needed."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    DRCOps,
    KiCadBackend,
    LibraryManageOps,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import GitOperationError, LibraryImportError, LibraryManageError
from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.sexp_parser import (
    _walk_balanced_parens,
    extract_sexp_block,
    find_footprint_block_by_reference,
    find_no_connect_block_by_position,
    find_symbol_block_by_reference,
    find_wire_block_by_endpoints,
    parse_sexp_file,
    remove_sexp_block,
)

logger = get_logger("backend.file")


class FileBoardOps(BoardOps):
    """Read-only board operations via direct file parsing."""

    def read_board(self, path: Path) -> dict[str, Any]:
        info = self.get_board_info(path)
        components = self.get_components(path)
        nets = self.get_nets(path)
        tracks = self.get_tracks(path)
        return {
            "info": info,
            "components": components,
            "nets": nets,
            "tracks": tracks,
        }

    def get_board_info(self, path: Path) -> dict[str, Any]:
        tree = parse_sexp_file(path)
        info: dict[str, Any] = {"file_path": str(path)}

        for node in tree:
            if not isinstance(node, list):
                continue
            if len(node) < 2:
                continue
            tag = node[0] if isinstance(node[0], str) else ""
            if tag == "title_block":
                info.update(_parse_title_block(node))
            elif tag == "paper":
                info["page_size"] = node[1] if len(node) > 1 else "A4"
            elif tag == "layers":
                info["layers"] = _parse_layers(node)

        # Count elements
        info["num_components"] = len(self.get_components(path))
        info["num_nets"] = len(self.get_nets(path))
        info["num_tracks"] = len(self.get_tracks(path))
        return info

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        tree = parse_sexp_file(path)
        components = []
        for node in tree:
            if isinstance(node, list) and len(node) > 0 and node[0] == "footprint":
                comp = _parse_footprint(node)
                if comp:
                    components.append(comp)
        return components

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        tree = parse_sexp_file(path)
        nets = []
        for node in tree:
            if isinstance(node, list) and len(node) >= 3 and node[0] == "net":
                nets.append({"number": node[1], "name": node[2]})
        return nets

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        tree = parse_sexp_file(path)
        tracks = []
        for node in tree:
            if isinstance(node, list) and len(node) > 0 and node[0] == "segment":
                track = _parse_segment(node)
                if track:
                    tracks.append(track)
        return tracks

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        tree = parse_sexp_file(path)
        for node in tree:
            if isinstance(node, list) and len(node) > 0 and node[0] == "setup":
                return _parse_setup(node)
        return {}

    @staticmethod
    def _resolve_net_id(content: str, net_name: str) -> tuple[str, int]:
        """Resolve a net name to its numeric ID in the PCB file.

        Scans for ``(net N "name")`` patterns. If the net is not found,
        adds a new net entry and returns its ID.

        Returns:
            Tuple of (possibly modified content, net ID).
        """
        if not net_name:
            return content, 0

        # Find top-level net definitions only (one per line), not pad-level net clauses.
        net_pattern = re.compile(r'(?m)^[ \t]*\(net\s+(\d+)\s+"([^"]*?)"\)\s*$')
        max_id = 0
        for m in net_pattern.finditer(content):
            net_id = int(m.group(1))
            if net_id > max_id:
                max_id = net_id
            if m.group(2) == net_name:
                return content, net_id

        # Net not found — add a new entry
        new_id = max_id + 1
        net_entry = f'  (net {new_id} "{net_name}")\n'
        # Insert after the last existing net entry, or before the first footprint
        last_net = None
        for m in net_pattern.finditer(content):
            last_net = m
        if last_net:
            insert_pos = last_net.end()
            content = content[:insert_pos] + "\n" + net_entry + content[insert_pos:]
        else:
            # Insert before the first footprint or before final paren
            fp_idx = content.find("(footprint ")
            if fp_idx != -1:
                content = content[:fp_idx] + net_entry + "\n" + content[fp_idx:]
            else:
                last_paren = content.rfind(")")
                if last_paren >= 0:
                    content = content[:last_paren] + net_entry + content[last_paren:]
        return content, new_id

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        import uuid
        fp_uuid = str(uuid.uuid4())

        rot_clause = f" {rotation}" if rotation else ""
        fp_sexp = (
            f'  (footprint "{footprint}" (layer "{layer}")\n'
            f'    (at {x} {y}{rot_clause})\n'
            f'    (property "Reference" "{reference}" (at 0 0 0)\n'
            f'      (effects (font (size 1 1) (thickness 0.15)))\n'
            f'    )\n'
            f'    (property "Value" "" (at 0 0 0)\n'
            f'      (effects (font (size 1 1) (thickness 0.15)))\n'
            f'    )\n'
            f'    (uuid "{fp_uuid}")\n'
            f'  )\n'
        )

        content = path.read_text(encoding="utf-8")
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + fp_sexp + content[last_paren:]
            path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "footprint": footprint,
            "position": {"x": x, "y": y},
            "layer": layer,
            "rotation": rotation,
            "uuid": fp_uuid,
        }

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_footprint_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Footprint with reference '{reference}' not found in {path}")
        start, end = location
        block = content[start:end + 1]

        # Find the footprint-level (at x y [rot]) — first occurrence
        at_match = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)', block)
        if at_match is None:
            raise ValueError(f"Footprint '{reference}' has no (at ...) clause")

        old_rot = float(at_match.group(3)) if at_match.group(3) else 0.0
        new_rot = rotation if rotation is not None else old_rot

        rot_clause = f" {new_rot}" if new_rot else ""
        new_at = f"(at {x} {y}{rot_clause})"
        new_block = block[:at_match.start()] + new_at + block[at_match.end():]

        content = content[:start] + new_block + content[end + 1:]
        path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "position": {"x": x, "y": y},
            "rotation": new_rot,
        }

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        import uuid
        track_uuid = str(uuid.uuid4())

        content = path.read_text(encoding="utf-8")
        content, net_id = self._resolve_net_id(content, net)

        track_sexp = (
            f'  (segment (start {start_x} {start_y}) (end {end_x} {end_y})'
            f' (width {width}) (layer "{layer}") (net {net_id})'
            f' (uuid "{track_uuid}"))\n'
        )

        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + track_sexp + content[last_paren:]
            path.write_text(content, encoding="utf-8")

        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "width": width,
            "layer": layer,
            "net": net,
            "uuid": track_uuid,
        }

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        import uuid
        via_uuid = str(uuid.uuid4())

        content = path.read_text(encoding="utf-8")
        content, net_id = self._resolve_net_id(content, net)

        via_sexp = (
            f'  (via (at {x} {y}) (size {size}) (drill {drill})'
            f' (layers "F.Cu" "B.Cu") (net {net_id})'
            f' (uuid "{via_uuid}"))\n'
        )

        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + via_sexp + content[last_paren:]
            path.write_text(content, encoding="utf-8")

        return {
            "position": {"x": x, "y": y},
            "size": size,
            "drill": drill,
            "net": net,
            "via_type": via_type,
            "uuid": via_uuid,
        }

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_footprint_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Footprint with reference '{reference}' not found in {path}")

        content, net_id = self._resolve_net_id(content, net)
        # Re-locate after possible content modification from _resolve_net_id
        location = find_footprint_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Footprint with reference '{reference}' not found after net resolve")

        start, end = location
        block = content[start:end + 1]

        # Find the pad sub-block
        escaped_pad = re.escape(pad)
        pad_pattern = re.compile(rf'\(pad\s+"{escaped_pad}"\s')
        pad_match = pad_pattern.search(block)
        if pad_match is None:
            # Try unquoted pad number
            pad_pattern = re.compile(rf'\(pad\s+{escaped_pad}\s')
            pad_match = pad_pattern.search(block)
        if pad_match is None:
            raise ValueError(f"Pad '{pad}' not found in footprint '{reference}'")

        pad_start = pad_match.start()
        pad_end_abs = _walk_balanced_parens(block, pad_start)
        if pad_end_abs is None:
            raise ValueError(f"Unbalanced pad block for pad '{pad}' in '{reference}'")

        pad_block = block[pad_start:pad_end_abs + 1]

        # Replace or insert (net ...) in the pad block
        net_in_pad = re.search(r'\(net\s+\d+(?:\s+"[^"]*")?\)', pad_block)
        net_clause = f'(net {net_id} "{net}")'

        if net_in_pad:
            new_pad_block = pad_block[:net_in_pad.start()] + net_clause + pad_block[net_in_pad.end():]
        else:
            # Insert before the closing paren of the pad block
            new_pad_block = pad_block[:-1] + f" {net_clause})"

        new_block = block[:pad_start] + new_pad_block + block[pad_end_abs + 1:]
        content = content[:start] + new_block + content[end + 1:]
        path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "pad": pad,
            "net": net,
        }


class FileSchematicOps(SchematicOps):
    """Read-only schematic operations via kicad-skip or direct parsing."""

    def read_schematic(self, path: Path) -> dict[str, Any]:
        try:
            from skip import Schematic
            sch = Schematic(str(path))
            return self._read_with_skip(sch, path)
        except ImportError:
            return self._read_with_sexp(path)
        except Exception:
            # skip library can fail on extended/custom symbols; fall back
            return self._read_with_sexp(path)

    @staticmethod
    def _skip_at_to_pos(at: Any) -> dict[str, float] | None:
        """Extract x,y position from a kicad-skip 'at' ParsedValue."""
        if hasattr(at, "value") and isinstance(at.value, list) and len(at.value) >= 2:
            return {"x": float(at.value[0]), "y": float(at.value[1])}
        if hasattr(at, "x") and hasattr(at, "y"):
            return {"x": float(at.x), "y": float(at.y)}
        return None

    def _read_with_skip(self, sch: Any, path: Path) -> dict[str, Any]:
        symbols = []
        for sym in getattr(sch, "symbol", []):
            symbol_data: dict[str, Any] = {}
            props = getattr(sym, "property", [])
            for prop in props:
                prop_key = None
                if hasattr(prop, "children") and len(prop.children) >= 1:
                    prop_key = prop.children[0]
                elif hasattr(prop, "key"):
                    prop_key = prop.key
                if prop_key and hasattr(prop, "value"):
                    if prop_key == "Reference":
                        symbol_data["reference"] = prop.value
                    elif prop_key == "Value":
                        symbol_data["value"] = prop.value
                    elif prop_key == "Footprint":
                        symbol_data["footprint"] = prop.value
            if hasattr(sym, "lib_id"):
                lib_id_str = str(sym.lib_id)
                symbol_data["lib_id"] = lib_id_str
                symbol_data["is_power"] = lib_id_str.startswith("power:")
            if hasattr(sym, "at"):
                pos = self._skip_at_to_pos(sym.at)
                if pos:
                    symbol_data["position"] = pos
            symbols.append(symbol_data)

        wires = []
        for wire in getattr(sch, "wire", []):
            if hasattr(wire, "pts"):
                pts = wire.pts
                if hasattr(pts, "xy") and len(pts.xy) >= 2:
                    p0 = pts.xy[0].value if hasattr(pts.xy[0], "value") else pts.xy[0]
                    p1 = pts.xy[1].value if hasattr(pts.xy[1], "value") else pts.xy[1]
                    wires.append({
                        "start": {"x": float(p0[0]), "y": float(p0[1])},
                        "end": {"x": float(p1[0]), "y": float(p1[1])},
                    })

        labels = []
        for label_type in ["label", "global_label", "hierarchical_label"]:
            for lbl in getattr(sch, label_type, []):
                label_data: dict[str, Any] = {"label_type": label_type}
                text_value = getattr(lbl, "text", None) if hasattr(lbl, "text") else None
                name_value = getattr(lbl, "name", None) if hasattr(lbl, "name") else None
                if text_value not in (None, ""):
                    label_data["text"] = str(text_value)
                elif name_value not in (None, ""):
                    label_data["text"] = str(name_value)
                else:
                    # Skip malformed/empty labels instead of inventing a literal "None" net name.
                    continue
                if hasattr(lbl, "at"):
                    pos = self._skip_at_to_pos(lbl.at)
                    if pos:
                        label_data["position"] = pos
                labels.append(label_data)

        # Parse no_connects, junctions, and sheets via sexp fallback (skip doesn't expose these well)
        no_connects = []
        junctions = []
        sheets = []
        try:
            tree = parse_sexp_file(path)
            for node in tree:
                if not isinstance(node, list) or len(node) < 1:
                    continue
                tag = node[0] if isinstance(node[0], str) else ""
                if tag == "no_connect":
                    nc = _parse_position_node(node)
                    if nc:
                        no_connects.append(nc)
                elif tag == "junction":
                    jn = _parse_position_node(node)
                    if jn:
                        junctions.append(jn)
                elif tag == "sheet":
                    sh = _parse_sheet_node(node)
                    if sh:
                        sheets.append(sh)
        except Exception:
            pass  # Non-critical, skip if parsing fails

        return {
            "info": {
                "file_path": str(path),
                "num_symbols": len(symbols),
                "num_wires": len(wires),
                "num_labels": len(labels),
                "num_no_connects": len(no_connects),
                "num_junctions": len(junctions),
                "num_sheets": len(sheets),
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
            "no_connects": no_connects,
            "junctions": junctions,
            "sheets": sheets,
        }

    def _read_with_sexp(self, path: Path) -> dict[str, Any]:
        tree = parse_sexp_file(path)
        symbols = []
        wires = []
        labels = []
        no_connects = []
        junctions = []
        sheets = []

        for node in tree:
            if not isinstance(node, list) or len(node) < 1:
                continue
            tag = node[0] if isinstance(node[0], str) else ""
            if tag == "symbol":
                sym = _parse_sch_symbol(node)
                if sym:
                    symbols.append(sym)
            elif tag == "wire":
                wire = _parse_sch_wire(node)
                if wire:
                    wires.append(wire)
            elif tag in ("label", "global_label", "hierarchical_label"):
                lbl = _parse_sch_label(node, tag)
                if lbl:
                    labels.append(lbl)
            elif tag == "no_connect":
                nc = _parse_position_node(node)
                if nc:
                    no_connects.append(nc)
            elif tag == "junction":
                jn = _parse_position_node(node)
                if jn:
                    junctions.append(jn)
            elif tag == "sheet":
                sh = _parse_sheet_node(node)
                if sh:
                    sheets.append(sh)

        return {
            "info": {
                "file_path": str(path),
                "num_symbols": len(symbols),
                "num_wires": len(wires),
                "num_labels": len(labels),
                "num_no_connects": len(no_connects),
                "num_junctions": len(junctions),
                "num_sheets": len(sheets),
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
            "no_connects": no_connects,
            "junctions": junctions,
            "sheets": sheets,
        }

    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        result = self.read_schematic(path)
        return result.get("symbols", [])

    def _resolve_symbol_libs(self) -> list[Path]:
        """Lazily resolve system symbol library paths (avoids scanning at construction)."""
        if not hasattr(self, '_symbol_libs'):
            from kicad_mcp.utils.kicad_paths import find_symbol_libraries
            self._symbol_libs = find_symbol_libraries()
        return self._symbol_libs

    def _get_project_symbol_libs(self, schematic_path: Path) -> list[Path]:
        """Read a project-level sym-lib-table and return resolved library paths.

        Supports ${PROJ_DIR} and ${KIPRJMOD} variable substitution.
        """
        result: list[Path] = []
        proj_dir = schematic_path.parent
        table_path = proj_dir / "sym-lib-table"
        if not table_path.exists():
            return result
        try:
            table_content = table_path.read_text(encoding="utf-8")
            for m in re.finditer(r'\(uri\s+"([^"]+)"\)', table_content):
                uri = m.group(1)
                uri = uri.replace("${PROJ_DIR}", str(proj_dir))
                uri = uri.replace("${KIPRJMOD}", str(proj_dir))
                p = Path(uri)
                if p.exists():
                    result.append(p)
        except Exception:
            pass
        return result

    def _ensure_lib_symbol_cached(
        self, content: str, lib_id: str,
        schematic_path: "Path | None" = None,
    ) -> str:
        """Inject a symbol definition into the schematic's lib_symbols section if missing.

        Resolves the library file from system paths (and, optionally, from the
        project-level sym-lib-table when *schematic_path* is supplied), extracts
        the symbol definition, renames it from plain name to full lib_id
        (e.g. "R" -> "Device:R"), and inserts it into the lib_symbols section.

        Returns the (possibly modified) schematic content.
        """
        # Split lib_id into library name and symbol name
        parts = lib_id.split(":", 1)
        if len(parts) != 2:
            logger.warning("Invalid lib_id format for lib_symbols cache: %s", lib_id)
            return content

        lib_name, sym_name = parts

        # Find the lib_symbols section
        lib_sym_start = content.find("(lib_symbols")
        if lib_sym_start == -1:
            # No lib_symbols section — create one before the first (symbol instance
            first_sym = content.find("(symbol ")
            if first_sym == -1:
                # No symbols at all yet, insert before closing paren
                last_paren = content.rfind(")")
                if last_paren >= 0:
                    content = content[:last_paren] + "  (lib_symbols\n  )\n" + content[last_paren:]
                else:
                    return content
            else:
                content = content[:first_sym] + "(lib_symbols\n  )\n  " + content[first_sym:]
            lib_sym_start = content.find("(lib_symbols")

        # Walk balanced parens to find the end of lib_symbols section
        from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
        lib_sym_end = _walk_balanced_parens(content, lib_sym_start)
        if lib_sym_end is None:
            logger.warning("Unbalanced lib_symbols section in schematic")
            return content

        lib_sym_section = content[lib_sym_start:lib_sym_end + 1]

        # Check if symbol is already cached
        escaped_lib_id = re.escape(lib_id)
        if re.search(rf'\(symbol\s+"{escaped_lib_id}"', lib_sym_section):
            return content

        # Find the library file — check system libs first, then project sym-lib-table
        lib_path = None
        for p in self._resolve_symbol_libs():
            if p.stem == lib_name:
                lib_path = p
                break

        if lib_path is None and schematic_path is not None:
            for p in self._get_project_symbol_libs(schematic_path):
                if p.stem == lib_name:
                    lib_path = p
                    break

        if lib_path is None:
            logger.warning("Library file not found for '%s', skipping lib_symbols cache", lib_name)
            return content

        # Extract the symbol definition from the library file
        try:
            lib_content = lib_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Cannot read library file: %s", lib_path)
            return content

        block = extract_sexp_block(lib_content, "symbol", sym_name)
        if block is None:
            logger.warning("Symbol '%s' not found in library %s", sym_name, lib_path)
            return content

        # Rename: replace the top-level symbol name and all sub-symbol references
        # Top-level: (symbol "R" -> (symbol "Device:R"
        escaped_sym = re.escape(sym_name)
        block = re.sub(
            rf'\(symbol\s+"{escaped_sym}"',
            f'(symbol "{lib_id}"',
            block,
            count=1,
        )
        # Sub-symbols keep the plain symbol name (no library prefix).
        # KiCad 9 format: outer symbol is "Device:R", sub-symbols stay "R_0_1",
        # "R_1_1", etc.  Prefixing sub-symbols with the lib name is invalid.
        # (No replacement needed — leave sub-symbol names as-is.)

        # Insert the block before the closing ) of lib_symbols
        indent_block = "    " + block.replace("\n", "\n    ")
        new_content = (
            content[:lib_sym_end]
            + "\n" + indent_block + "\n  "
            + content[lib_sym_end:]
        )
        return new_content

    @staticmethod
    def _find_insertion_point(content: str) -> int:
        """Finds the appropriate insertion point for new schematic elements.

        This is typically after the (lib_symbols ...) block and before
        the (sheet_instances ...) block, or before the final closing paren.
        """
        lib_sym_end = content.find("(lib_symbols")
        if lib_sym_end != -1:
            lib_sym_end = _walk_balanced_parens(content, lib_sym_end)
            if lib_sym_end is not None:
                # Insert after the lib_symbols block, plus its closing paren
                return lib_sym_end + 1

        # Fallback: insert before the last closing parenthesis
        last_paren = content.rfind(")")
        if last_paren >= 0:
            return last_paren
        return len(content) # Should not happen for a valid schematic

    @staticmethod
    def _find_schematic_uuid(content: str) -> str:
        """Extract the root schematic UUID from file content."""
        m = re.search(r'\(uuid\s+"([^"]+)"\)', content)
        return m.group(1) if m else ""

    def create_schematic(
        self, path: Path, title: str = "", revision: str = "",
    ) -> dict[str, Any]:
        import uuid as _uuid
        sch_uuid = str(_uuid.uuid4())

        title_block = ""
        if title or revision:
            tb_lines = []
            if title:
                tb_lines.append(f'    (title "{title}")')
            if revision:
                tb_lines.append(f'    (rev "{revision}")')
            title_block = (
                "  (title_block\n"
                + "\n".join(tb_lines)
                + "\n  )\n\n"
            )

        content = (
            f'(kicad_sch\n'
            f'  (version 20231120)\n'
            f'  (generator "kicad_mcp")\n'
            f'  (generator_version "9.0")\n'
            f'  (uuid "{sch_uuid}")\n'
            f'\n'
            f'  (paper "A4")\n'
            f'\n'
            f'{title_block}'
            f'  (lib_symbols\n'
            f'  )\n'
            f'\n'
            f'  (sheet_instances\n'
            f'    (path "/" (page "1"))\n'
            f'  )\n'
            f')\n'
        )

        path.write_text(content, encoding="utf-8")
        return {
            "path": str(path),
            "uuid": sch_uuid,
            "title": title,
            "revision": revision,
        }

    def add_component(
        self, path: Path, lib_id: str, reference: str, value: str,
        x: float, y: float, rotation: float = 0.0,
        mirror: str | None = None, footprint: str = "",
        properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        import uuid
        symbol_uuid = str(uuid.uuid4())

        # Build at clause
        at_clause = f"(at {x} {y} {rotation})"

        # Build mirror clause
        mirror_clause = ""
        if mirror in ("x", "y"):
            mirror_clause = f"\n    (mirror {mirror})"

        # Build properties
        prop_lines = (
            f'    (property "Reference" "{reference}" (at {x} {y - 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
            f'    (property "Value" "{value}" (at {x} {y + 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
        )

        if footprint:
            prop_lines += (
                f'    (property "Footprint" "{footprint}" (at {x} {y + 4} 0)\n'
                f'      (effects (font (size 1.27 1.27)) hide)\n'
                f'    )\n'
            )

        if properties:
            offset = 6
            for prop_name, prop_val in properties.items():
                prop_lines += (
                    f'    (property "{prop_name}" "{prop_val}" (at {x} {y + offset} 0)\n'
                    f'      (effects (font (size 1.27 1.27)) hide)\n'
                    f'    )\n'
                )
                offset += 2

        content = path.read_text(encoding="utf-8")
        content = self._ensure_lib_symbol_cached(content, lib_id, schematic_path=path)
        sch_uuid = self._find_schematic_uuid(content)

        sym_sexp = (
            f'  (symbol (lib_id "{lib_id}") {at_clause}{mirror_clause} (unit 1)\n'
            f'    (in_bom yes) (on_board yes) (dnp no)\n'
            f'    (uuid "{symbol_uuid}")\n'
            f'{prop_lines}'
            f'    (instances\n'
            f'      (project ""\n'
            f'        (path "/{sch_uuid}"\n'
            f'          (reference "{reference}") (unit 1)\n'
            f'        )\n'
            f'      )\n'
            f'    )\n'
            f'  )\n'
        )

        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + sym_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        result: dict[str, Any] = {
            "reference": reference,
            "value": value,
            "lib_id": lib_id,
            "position": {"x": x, "y": y},
            "rotation": rotation,
            "uuid": symbol_uuid,
        }
        if footprint:
            result["footprint"] = footprint
        if mirror:
            result["mirror"] = mirror
        return result

    def add_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        import uuid
        wire_uuid = str(uuid.uuid4())
        wire_sexp = (
            f'  (wire (pts (xy {start_x} {start_y}) (xy {end_x} {end_y}))\n'
            f'    (stroke (width 0) (type default))\n'
            f'    (uuid "{wire_uuid}")\n'
            f'  )\n'
        )

        content = path.read_text(encoding="utf-8")
        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + wire_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "uuid": wire_uuid,
        }

    def add_label(
        self, path: Path, text: str, x: float, y: float,
        label_type: str = "net_label",
    ) -> dict[str, Any]:
        import uuid
        label_uuid = str(uuid.uuid4())
        tag = label_type if label_type != "net_label" else "label"
        label_sexp = (
            f'  ({tag} "{text}" (at {x} {y} 0)\n'
            f'    (effects (font (size 1.27 1.27)))\n'
            f'    (uuid "{label_uuid}")\n'
            f'  )\n'
        )

        content = path.read_text(encoding="utf-8")
        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + label_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        return {
            "text": text,
            "position": {"x": x, "y": y},
            "label_type": label_type,
            "uuid": label_uuid,
        }

    def add_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        import uuid
        nc_uuid = str(uuid.uuid4())
        nc_sexp = (
            f'  (no_connect (at {x} {y}) (uuid "{nc_uuid}"))\n'
        )

        content = path.read_text(encoding="utf-8")
        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + nc_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        return {
            "position": {"x": x, "y": y},
            "uuid": nc_uuid,
        }

    def add_power_symbol(
        self, path: Path, name: str, x: float, y: float, rotation: float = 0.0,
    ) -> dict[str, Any]:
        import uuid
        symbol_uuid = str(uuid.uuid4())

        # Power symbols use lib_id "power:<name>" and Reference "#PWR0XX"
        # Auto-increment PWR reference by scanning existing ones
        content = path.read_text(encoding="utf-8")
        pwr_refs = re.findall(r'"#PWR(\d+)"', content)
        next_num = max((int(n) for n in pwr_refs), default=0) + 1
        pwr_ref = f"#PWR{next_num:03d}"

        lib_id = f"power:{name}"
        content = self._ensure_lib_symbol_cached(content, lib_id, schematic_path=path)

        sch_uuid = self._find_schematic_uuid(content)

        sym_sexp = (
            f'  (symbol (lib_id "{lib_id}") (at {x} {y} {rotation}) (unit 1)\n'
            f'    (in_bom yes) (on_board yes) (dnp no)\n'
            f'    (uuid "{symbol_uuid}")\n'
            f'    (property "Reference" "{pwr_ref}" (at {x} {y - 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)) hide)\n'
            f'    )\n'
            f'    (property "Value" "{name}" (at {x} {y + 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
            f'    (instances\n'
            f'      (project ""\n'
            f'        (path "/{sch_uuid}"\n'
            f'          (reference "{pwr_ref}") (unit 1)\n'
            f'        )\n'
            f'      )\n'
            f'    )\n'
            f'  )\n'
        )

        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + sym_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        return {
            "name": name,
            "lib_id": lib_id,
            "reference": pwr_ref,
            "position": {"x": x, "y": y},
            "rotation": rotation,
            "uuid": symbol_uuid,
        }

    def add_junction(self, path: Path, x: float, y: float) -> dict[str, Any]:
        import uuid
        jn_uuid = str(uuid.uuid4())
        jn_sexp = (
            f'  (junction (at {x} {y}) (diameter 0) (color 0 0 0 0)\n'
            f'    (uuid "{jn_uuid}")\n'
            f'  )\n'
        )

        content = path.read_text(encoding="utf-8")
        insert_pos = self._find_insertion_point(content)
        content = content[:insert_pos] + jn_sexp + content[insert_pos:]
        path.write_text(content, encoding="utf-8")

        return {
            "position": {"x": x, "y": y},
            "uuid": jn_uuid,
        }

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in {path}")
        start, end = location
        block = content[start:end + 1]

        # Parse the symbol-level (at old_x old_y old_rot) — first occurrence
        at_match = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)', block)
        if at_match is None:
            raise ValueError(f"Symbol '{reference}' has no (at ...) clause")

        old_x = float(at_match.group(1))
        old_y = float(at_match.group(2))
        old_rot = float(at_match.group(3)) if at_match.group(3) else 0.0
        new_rot = rotation if rotation is not None else old_rot

        dx = x - old_x
        dy = y - old_y

        # Replace the symbol-level (at ...) with new values
        new_at = f"(at {x} {y} {new_rot})"
        new_block = block[:at_match.start()] + new_at + block[at_match.end():]

        # Shift all property (at ...) positions by the same delta.
        # Properties look like: (property "Name" "Value" (at px py angle) ...)
        # We need to update each one. Process from end to start to preserve indices.
        prop_at_pattern = re.compile(
            r'(\(property\s+"[^"]*"\s+"[^"]*"\s+)\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)'
        )
        matches = list(prop_at_pattern.finditer(new_block))
        for m in reversed(matches):
            px = float(m.group(2)) + dx
            py = float(m.group(3)) + dy
            p_rot = m.group(4) if m.group(4) else "0"
            replacement = f"{m.group(1)}(at {px} {py} {p_rot})"
            new_block = new_block[:m.start()] + replacement + new_block[m.end():]

        content = content[:start] + new_block + content[end + 1:]
        path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "position": {"x": x, "y": y},
            "rotation": new_rot,
        }

    def update_component_property(
        self, path: Path, reference: str,
        property_name: str, property_value: str,
    ) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in {path}")
        start, end = location
        block = content[start:end + 1]

        # Try to find existing property with this name
        escaped_name = re.escape(property_name)
        prop_pattern = re.compile(
            rf'(\(property\s+"{escaped_name}"\s+)"([^"]*)"'
        )
        match = prop_pattern.search(block)

        if match:
            # Replace the existing value
            new_block = (
                block[:match.start(2)]
                + property_value
                + block[match.end(2):]
            )
        else:
            # Append a new property before the closing paren of the symbol block.
            # Find the symbol's position to place the property label nearby.
            at_match = re.search(
                r'\(at\s+([-\d.]+)\s+([-\d.]+)', block
            )
            px = float(at_match.group(1)) if at_match else 0
            py = float(at_match.group(2)) + 6 if at_match else 0

            new_prop = (
                f'    (property "{property_name}" "{property_value}" (at {px} {py} 0)\n'
                f'      (effects (font (size 1.27 1.27)) hide)\n'
                f'    )\n  '
            )
            # Insert before the final closing paren of the block
            new_block = block[:-1].rstrip() + "\n" + new_prop + ")"

        content = content[:start] + new_block + content[end + 1:]
        path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "property": property_name,
            "value": property_value,
        }

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in {path}")
        start, end = location
        content = remove_sexp_block(content, start, end)
        path.write_text(content, encoding="utf-8")
        return {"reference": reference, "removed": True}

    def remove_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_wire_block_by_endpoints(content, start_x, start_y, end_x, end_y)
        if location is None:
            raise ValueError(
                f"Wire from ({start_x}, {start_y}) to ({end_x}, {end_y}) not found in {path}"
            )
        start, end = location
        content = remove_sexp_block(content, start, end)
        path.write_text(content, encoding="utf-8")
        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "removed": True,
        }

    def remove_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_no_connect_block_by_position(content, x, y)
        if location is None:
            raise ValueError(
                f"No-connect at ({x}, {y}) not found in {path}"
            )
        start, end = location
        content = remove_sexp_block(content, start, end)
        path.write_text(content, encoding="utf-8")
        return {
            "position": {"x": x, "y": y},
            "removed": True,
        }

    def get_symbol_pin_positions(
        self, path: Path, reference: str,
    ) -> dict[str, Any]:
        import math

        tree = parse_sexp_file(path)

        # 1. Find the symbol instance by reference
        sym_node = None
        for node in tree:
            if not isinstance(node, list) or len(node) < 2:
                continue
            if node[0] != "symbol":
                continue
            for child in node[1:]:
                if (isinstance(child, list) and len(child) >= 3
                        and child[0] == "property" and child[1] == "Reference"
                        and child[2] == reference):
                    sym_node = node
                    break
            if sym_node is not None:
                break

        if sym_node is None:
            return {"error": f"Symbol with reference '{reference}' not found"}

        # Extract symbol placement: at, lib_id, mirror
        sx, sy, sym_rot = 0.0, 0.0, 0.0
        lib_id = ""
        sym_mirror = None
        for child in sym_node[1:]:
            if not isinstance(child, list) or len(child) < 2:
                continue
            tag = child[0] if isinstance(child[0], str) else ""
            if tag == "at" and len(child) >= 3:
                sx = float(child[1])
                sy = float(child[2])
                if len(child) >= 4:
                    sym_rot = float(child[3])
            elif tag == "lib_id":
                lib_id = child[1]
            elif tag == "mirror":
                sym_mirror = child[1]

        if not lib_id:
            return {"error": f"Symbol '{reference}' has no lib_id"}

        # 2. Find the lib_symbols entry in the schematic
        lib_symbols_node = None
        for node in tree:
            if isinstance(node, list) and len(node) >= 1 and node[0] == "lib_symbols":
                lib_symbols_node = node
                break

        if lib_symbols_node is None:
            return {"error": "No lib_symbols section found in schematic"}

        # Find the matching library symbol definition
        # The lib_symbols cache uses the lib_id directly as the symbol name
        lib_sym = None
        for child in lib_symbols_node[1:]:
            if (isinstance(child, list) and len(child) >= 2
                    and child[0] == "symbol" and child[1] == lib_id):
                lib_sym = child
                break

        if lib_sym is None:
            # lib_symbols cache is missing this symbol — try to read directly from
            # the library file (either system or project sym-lib-table).
            lib_name_fallback = lib_id.split(":", 1)[0] if ":" in lib_id else lib_id
            sym_name_fallback = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
            lib_path_fallback: "Path | None" = None
            for lp in self._resolve_symbol_libs():
                if lp.stem == lib_name_fallback:
                    lib_path_fallback = lp
                    break
            if lib_path_fallback is None:
                for lp in self._get_project_symbol_libs(path):
                    if lp.stem == lib_name_fallback:
                        lib_path_fallback = lp
                        break
            if lib_path_fallback is not None:
                try:
                    fallback_tree = parse_sexp_file(lib_path_fallback)
                    for node in fallback_tree:
                        if (isinstance(node, list) and len(node) >= 2
                                and node[0] == "symbol"
                                and node[1] == sym_name_fallback):
                            lib_sym = node
                            break
                except Exception:
                    pass
            if lib_sym is None:
                return {"error": f"Library symbol '{lib_id}' not found in lib_symbols cache"}

        # 3. Extract pins from lib symbol (recurse into sub-symbols).
        # If the cached symbol uses (extends "ParentName"), the pin definitions
        # live in the parent symbol inside the source .kicad_sym library file.
        def _collect_pins_from_node(node: list) -> list[dict]:
            collected: list[dict] = []
            for child in node[1:]:
                if not isinstance(child, list) or len(child) < 2:
                    continue
                if child[0] == "pin":
                    collected.append(_parse_pin_node(child))
                elif child[0] == "symbol":
                    for sub_child in child[1:]:
                        if (isinstance(sub_child, list) and len(sub_child) >= 2
                                and sub_child[0] == "pin"):
                            collected.append(_parse_pin_node(sub_child))
            return collected

        pins = _collect_pins_from_node(lib_sym)

        if not pins:
            # Check for (extends "ParentName") and resolve from source library
            parent_name: str | None = None
            for child in lib_sym[1:]:
                if isinstance(child, list) and len(child) >= 2 and child[0] == "extends":
                    parent_name = child[1]
                    break

            if parent_name is not None:
                lib_name = lib_id.split(":", 1)[0] if ":" in lib_id else lib_id
                lib_path: Path | None = None
                for lp in self._resolve_symbol_libs():
                    if lp.stem == lib_name:
                        lib_path = lp
                        break

                if lib_path is not None:
                    try:
                        lib_tree = parse_sexp_file(lib_path)
                        # Follow extends chain (up to 5 levels deep)
                        resolved_name = parent_name
                        for _ in range(5):
                            parent_node: list | None = None
                            for node in lib_tree:
                                if (isinstance(node, list) and len(node) >= 2
                                        and node[0] == "symbol"
                                        and node[1] == resolved_name):
                                    parent_node = node
                                    break
                            if parent_node is None:
                                break
                            pins = _collect_pins_from_node(parent_node)
                            if pins:
                                break
                            # Look for another level of extends
                            next_parent: str | None = None
                            for child in parent_node[1:]:
                                if (isinstance(child, list) and len(child) >= 2
                                        and child[0] == "extends"):
                                    next_parent = child[1]
                                    break
                            if next_parent is None:
                                break
                            resolved_name = next_parent
                    except Exception:
                        pass

        # 4. Transform pin positions to absolute schematic coordinates
        rad = math.radians(sym_rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        pin_positions: dict[str, dict[str, float]] = {}
        for pin in pins:
            pos = pin.get("position")
            if pos is None:
                continue
            px = pos["x"]
            py = pos["y"]

            # Library coordinates are Y-up, schematic is Y-down, so negate py
            py_sch = -py

            # Apply mirror before rotation
            if sym_mirror == "x":
                py_sch = -py_sch
            elif sym_mirror == "y":
                px = -px

            # Apply rotation
            abs_x = sx + px * cos_r - py_sch * sin_r
            abs_y = sy + px * sin_r + py_sch * cos_r

            pin_key = pin.get("number", pin.get("name", ""))
            if pin_key:
                pin_positions[pin_key] = {
                    "x": round(abs_x, 4),
                    "y": round(abs_y, 4),
                }

        return {
            "reference": reference,
            "lib_id": lib_id,
            "position": {"x": sx, "y": sy},
            "rotation": sym_rot,
            "mirror": sym_mirror,
            "pin_positions": pin_positions,
        }


    def _build_connectivity(self, path: Path) -> dict[str, list[dict[str, Any]]]:
        """Build schematic net connectivity from wires, labels, and pin positions.

        Uses a Union-Find over coordinate endpoints to group connected items
        into nets, then names each group from labels or power symbols.

        Returns:
            Mapping of net_name -> list of {reference, pin_number, position}.
        """
        # Connectivity needs exact wire/label geometry; use sexp parsing directly
        # instead of the optional skip parser to avoid lossy label handling.
        data = self._read_with_sexp(path)
        symbols = data.get("symbols", [])
        wires = data.get("wires", [])
        labels = data.get("labels", [])

        TOLERANCE = 0.02  # mm coordinate matching tolerance

        # --- Union-Find ---
        parent: dict[str, str] = {}

        def _key(x: float, y: float) -> str:
            return f"{round(x / TOLERANCE) * TOLERANCE:.4f},{round(y / TOLERANCE) * TOLERANCE:.4f}"

        def find(k: str) -> str:
            while parent.get(k, k) != k:
                parent[k] = parent.get(parent[k], parent[k])
                k = parent[k]
            return k

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Collect wire endpoints and union them
        for w in wires:
            s = w.get("start", {})
            e = w.get("end", {})
            sk = _key(s.get("x", 0), s.get("y", 0))
            ek = _key(e.get("x", 0), e.get("y", 0))
            parent.setdefault(sk, sk)
            parent.setdefault(ek, ek)
            union(sk, ek)

        # Collect label positions -> net names
        label_names: dict[str, str] = {}  # key -> net_name
        for lbl in labels:
            pos = lbl.get("position", {})
            lk = _key(pos.get("x", 0), pos.get("y", 0))
            parent.setdefault(lk, lk)
            label_names[lk] = lbl.get("text", "")
            # Union with any wire at same position
            for existing in list(parent):
                if existing != lk and find(existing) != find(lk):
                    # Check if any existing endpoint matches
                    pass
            # Just add to parent set; union happens via wire overlap

        # Collect pin positions for all non-power symbols
        pin_data: list[dict[str, Any]] = []  # {reference, pin_number, position, key}
        power_pin_names: dict[str, str] = {}  # key -> power net name

        for sym in symbols:
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#"):
                continue
            is_power = sym.get("is_power", False)

            try:
                pin_result = self.get_symbol_pin_positions(path, ref)
            except Exception:
                continue

            pin_positions = pin_result.get("pin_positions", {})
            for pin_num, pos in pin_positions.items():
                pk = _key(pos["x"], pos["y"])
                parent.setdefault(pk, pk)

                if is_power:
                    # Power symbol pin defines a net name (use Value)
                    power_pin_names[pk] = sym.get("value", ref)
                else:
                    pin_data.append({
                        "reference": ref,
                        "pin_number": pin_num,
                        "position": pos,
                        "key": pk,
                    })

        # Also handle power symbols with # refs
        for sym in symbols:
            ref = sym.get("reference", "")
            is_power = sym.get("is_power", False)
            if not is_power and not ref.startswith("#"):
                continue
            if not ref:
                continue
            try:
                pin_result = self.get_symbol_pin_positions(path, ref)
            except Exception:
                continue
            pin_positions = pin_result.get("pin_positions", {})
            for pin_num, pos in pin_positions.items():
                pk = _key(pos["x"], pos["y"])
                parent.setdefault(pk, pk)
                power_pin_names[pk] = sym.get("value", ref)

        # Union all points at the same coordinates
        all_keys = list(parent.keys())
        for pk in [p["key"] for p in pin_data] + list(label_names) + list(power_pin_names):
            for k2 in all_keys:
                if pk == k2:
                    continue
                if find(pk) == find(k2):
                    continue
                # They are at the same rounded coordinate — already same key
            # Keys are already rounded, so same position = same key = implicitly unioned
            # But we need to union pin keys with wire endpoint keys
            if pk in parent:
                union(pk, pk)  # no-op, but ensures it's in parent

        # Build groups by root
        groups: dict[str, list[str]] = {}
        for k in parent:
            root = find(k)
            groups.setdefault(root, []).append(k)

        # Name each group
        net_map: dict[str, list[dict[str, Any]]] = {}
        for root, members in groups.items():
            # Determine net name from labels or power symbols
            net_name = ""
            for m in members:
                if m in label_names:
                    net_name = label_names[m]
                    break
                if m in power_pin_names:
                    net_name = power_pin_names[m]
                    break

            # Find pins in this group
            group_pins = []
            for pd in pin_data:
                if find(pd["key"]) == root:
                    group_pins.append({
                        "reference": pd["reference"],
                        "pin_number": pd["pin_number"],
                        "position": pd["position"],
                    })

            if not group_pins:
                continue

            if not net_name:
                # Auto-name from first pin
                p = group_pins[0]
                net_name = f"Net-({p['reference']}-{p['pin_number']})"

            net_map.setdefault(net_name, []).extend(group_pins)

        return net_map

    def get_pin_net(self, path: Path, reference: str, pin_number: str) -> dict[str, Any]:
        connectivity = self._build_connectivity(path)

        for net_name, pins in connectivity.items():
            for pin in pins:
                if pin["reference"] == reference and str(pin["pin_number"]) == str(pin_number):
                    return {
                        "reference": reference,
                        "pin_number": pin_number,
                        "net_name": net_name,
                        "position": pin["position"],
                    }

        return {
            "reference": reference,
            "pin_number": pin_number,
            "net_name": None,
            "error": f"Pin {pin_number} of {reference} not found in connectivity map",
        }

    def get_net_connections(self, path: Path, net_name: str) -> dict[str, Any]:
        connectivity = self._build_connectivity(path)

        if net_name not in connectivity:
            return {
                "net_name": net_name,
                "pins": [],
                "error": f"Net '{net_name}' not found in schematic connectivity",
            }

        data = self.read_schematic(path)
        labels_on_net = []
        for lbl in data.get("labels", []):
            if lbl.get("text") == net_name:
                labels_on_net.append(lbl)

        wires_on_net = []  # Simplified: return all wires (full wire-to-net mapping is complex)

        return {
            "net_name": net_name,
            "pins": connectivity[net_name],
            "labels": labels_on_net,
            "wires": wires_on_net,
        }


    def get_sheet_hierarchy(self, path: Path) -> dict[str, Any]:
        """Recursively read the hierarchical sheet tree from a root schematic."""
        visited: set[str] = set()

        def _build_tree(sch_path: Path) -> dict[str, Any]:
            resolved = str(sch_path.resolve())
            if resolved in visited:
                return {
                    "name": sch_path.stem,
                    "file": str(sch_path),
                    "error": "circular reference detected",
                    "sheets": [],
                }
            visited.add(resolved)

            try:
                data = self.read_schematic(sch_path)
            except Exception as exc:
                return {
                    "name": sch_path.stem,
                    "file": str(sch_path),
                    "error": str(exc),
                    "sheets": [],
                }

            info = data.get("info", {})
            sheets_data = data.get("sheets", [])
            children = []

            for sh in sheets_data:
                sheetfile = sh.get("sheetfile", "")
                if not sheetfile:
                    continue
                # Resolve relative to parent schematic directory
                child_path = sch_path.parent / sheetfile
                if child_path.exists():
                    child_tree = _build_tree(child_path)
                    child_tree["name"] = sh.get("sheetname", child_path.stem)
                    child_tree["pins"] = sh.get("pins", [])
                    children.append(child_tree)
                else:
                    children.append({
                        "name": sh.get("sheetname", sheetfile),
                        "file": str(child_path),
                        "error": "file not found",
                        "sheets": [],
                    })

            return {
                "name": sch_path.stem,
                "file": str(sch_path),
                "symbols_count": info.get("num_symbols", 0),
                "wires_count": info.get("num_wires", 0),
                "labels_count": info.get("num_labels", 0),
                "sheets": children,
            }

        return _build_tree(path)

    def validate_schematic(self, path: Path) -> dict[str, Any]:
        """File-based electrical rules validation (no kicad-cli needed).

        Checks for:
        1. Duplicate reference designators (error)
        2. Floating pins — not connected and no no-connect marker (warning)
        3. Missing power connections (warning)
        """
        data = self.read_schematic(path)
        symbols = data.get("symbols", [])
        no_connects = data.get("no_connects", [])

        violations: list[dict[str, Any]] = []
        error_count = 0
        warning_count = 0

        TOLERANCE = 0.02

        def _near(a: dict, b: dict) -> bool:
            return (abs(a.get("x", 0) - b.get("x", 0)) < TOLERANCE
                    and abs(a.get("y", 0) - b.get("y", 0)) < TOLERANCE)

        # --- Check 1: Duplicate reference designators ---
        ref_counts: dict[str, list[dict]] = {}
        for sym in symbols:
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#"):
                continue
            if sym.get("is_power"):
                continue
            ref_counts.setdefault(ref, []).append(sym)

        for ref, syms in ref_counts.items():
            if len(syms) > 1:
                positions = [s.get("position", {}) for s in syms]
                violations.append({
                    "severity": "error",
                    "type": "duplicate_reference",
                    "description": f"Duplicate reference designator '{ref}' ({len(syms)} instances)",
                    "reference": ref,
                    "positions": positions,
                })
                error_count += 1

        # --- Check 2: Floating pins ---
        # Build connectivity and find unconnected pins
        try:
            connectivity = self._build_connectivity(path)
        except Exception:
            connectivity = {}

        # Collect all connected pin keys (reference + pin_number)
        connected_pins: set[str] = set()
        for net_name, pins in connectivity.items():
            if len(pins) >= 2 or net_name:
                for pin in pins:
                    connected_pins.add(f"{pin['reference']}:{pin['pin_number']}")

        # Build set of no-connect positions
        nc_positions = [nc.get("position", {}) for nc in no_connects]

        # Check each non-power symbol's pins
        for sym in symbols:
            ref = sym.get("reference", "")
            if not ref or ref.startswith("#") or sym.get("is_power"):
                continue

            try:
                pin_result = self.get_symbol_pin_positions(path, ref)
            except Exception:
                continue

            pin_positions = pin_result.get("pin_positions", {})
            for pin_num, pos in pin_positions.items():
                pin_key = f"{ref}:{pin_num}"
                if pin_key in connected_pins:
                    continue

                # Check if there's a no-connect marker at this pin
                has_nc = any(_near(pos, nc_pos) for nc_pos in nc_positions)
                if has_nc:
                    continue

                # Check if it's in a net with at least one other pin
                in_any_net = False
                for net_name, pins in connectivity.items():
                    for pin in pins:
                        if pin["reference"] == ref and str(pin["pin_number"]) == str(pin_num):
                            if len(pins) >= 2:
                                in_any_net = True
                            break
                    if in_any_net:
                        break

                if not in_any_net:
                    violations.append({
                        "severity": "warning",
                        "type": "floating_pin",
                        "description": f"Pin {pin_num} of {ref} is not connected and has no no-connect marker",
                        "reference": ref,
                        "pin": pin_num,
                        "position": pos,
                    })
                    warning_count += 1

        # --- Check 3: Missing power connections ---
        # Check that power symbol pins are connected to at least one non-power component
        for sym in symbols:
            ref = sym.get("reference", "")
            is_power = sym.get("is_power", False)
            if not is_power and not ref.startswith("#"):
                continue
            if not ref:
                continue

            value = sym.get("value", ref)
            # Check if this power net has any non-power pins connected
            has_connections = False
            for net_name, pins in connectivity.items():
                if net_name == value and len(pins) > 0:
                    has_connections = True
                    break

            if not has_connections:
                violations.append({
                    "severity": "warning",
                    "type": "unconnected_power",
                    "description": f"Power symbol '{value}' ({ref}) is not connected to any component pins",
                    "reference": ref,
                    "position": sym.get("position", {}),
                })
                warning_count += 1

        return {
            "passed": error_count == 0,
            "violations": violations,
            "error_count": error_count,
            "warning_count": warning_count,
            "checks_performed": [
                "duplicate_reference",
                "floating_pin",
                "unconnected_power",
            ],
        }


class FileDRCOps:
    """File-based DRC/ERC operations (lite, no kicad-cli needed)."""

    def __init__(self, schematic_ops: FileSchematicOps) -> None:
        self._sch_ops = schematic_ops

    def run_erc(self, schematic_path: Path, output: Path | None = None) -> dict[str, Any]:
        """Run file-based ERC using validate_schematic."""
        result = self._sch_ops.validate_schematic(schematic_path)
        result["backend"] = "file"
        result["note"] = "File-based ERC lite. For full ERC, use kicad-cli backend."

        if output:
            import json
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["report_file"] = str(output)

        return result

    def run_drc(self, board_path: Path, output: Path | None = None) -> dict[str, Any]:
        raise NotImplementedError(
            "File-based DRC is not supported. Use kicad-cli backend for board DRC."
        )


class FileLibraryOps(LibraryOps):
    """Library operations via direct file searching."""

    def __init__(self) -> None:
        from kicad_mcp.utils.kicad_paths import find_footprint_libraries, find_symbol_libraries
        self._symbol_libs = find_symbol_libraries()
        self._footprint_libs = find_footprint_libraries()

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        results = []
        query_lower = query.lower()
        for lib_path in self._symbol_libs:
            lib_name = lib_path.stem
            try:
                tree = parse_sexp_file(lib_path)
                for node in tree:
                    if (isinstance(node, list) and len(node) >= 2
                            and node[0] == "symbol"):
                        sym_name = node[1] if isinstance(node[1], str) else ""
                        if query_lower in sym_name.lower():
                            results.append({
                                "name": sym_name,
                                "library": lib_name,
                                "lib_id": f"{lib_name}:{sym_name}",
                            })
            except Exception as e:
                logger.debug("Error reading symbol lib %s: %s", lib_path, e)
        return results[:50]  # Limit results

    def search_footprints(self, query: str) -> list[dict[str, Any]]:
        results = []
        query_lower = query.lower()
        for lib_dir in self._footprint_libs:
            lib_name = lib_dir.stem.replace(".pretty", "")
            for fp_file in lib_dir.glob("*.kicad_mod"):
                if query_lower in fp_file.stem.lower():
                    results.append({
                        "name": fp_file.stem,
                        "library": lib_name,
                        "lib_id": f"{lib_name}:{fp_file.stem}",
                    })
        return results[:50]

    def list_libraries(self) -> list[dict[str, Any]]:
        libs = []
        for lib_path in self._symbol_libs:
            libs.append({
                "name": lib_path.stem,
                "type": "symbol",
                "path": str(lib_path),
            })
        for lib_dir in self._footprint_libs:
            libs.append({
                "name": lib_dir.stem.replace(".pretty", ""),
                "type": "footprint",
                "path": str(lib_dir),
            })
        return libs

    def get_symbol_info(self, lib_id: str) -> dict[str, Any]:
        parts = lib_id.split(":", 1)
        if len(parts) != 2:
            return {"error": f"Invalid lib_id format: {lib_id}. Expected 'Library:Symbol'"}
        lib_name, sym_name = parts

        for lib_path in self._symbol_libs:
            if lib_path.stem == lib_name:
                tree = parse_sexp_file(lib_path)
                for node in tree:
                    if (isinstance(node, list) and len(node) >= 2
                            and node[0] == "symbol" and node[1] == sym_name):
                        return _parse_symbol_detail(node, lib_name)
        return {"error": f"Symbol not found: {lib_id}"}

    def get_footprint_info(self, lib_id: str) -> dict[str, Any]:
        parts = lib_id.split(":", 1)
        if len(parts) != 2:
            return {"error": f"Invalid lib_id format: {lib_id}. Expected 'Library:Footprint'"}
        lib_name, fp_name = parts

        for lib_dir in self._footprint_libs:
            if lib_dir.stem.replace(".pretty", "") == lib_name:
                fp_file = lib_dir / f"{fp_name}.kicad_mod"
                if fp_file.exists():
                    tree = parse_sexp_file(fp_file)
                    return _parse_footprint_detail(tree, lib_name, fp_name)
        return {"error": f"Footprint not found: {lib_id}"}

    def suggest_footprints(self, lib_id: str) -> dict[str, Any]:
        import fnmatch

        sym_info = self.get_symbol_info(lib_id)
        if "error" in sym_info:
            return sym_info

        fp_filters = sym_info.get("fp_filters", [])
        if not fp_filters:
            return {
                "lib_id": lib_id,
                "fp_filters": [],
                "footprints": [],
                "message": "Symbol has no footprint filters defined.",
            }

        # Iterate all footprint libraries directly to avoid the 50-result cap
        # imposed by search_footprints().  Cap results at 100 total.
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()

        for lib_dir in self._footprint_libs:
            lib_name = lib_dir.stem.replace(".pretty", "")
            for fp_file in lib_dir.glob("*.kicad_mod"):
                fp_name = fp_file.stem
                if fp_name in seen:
                    continue
                for pattern in fp_filters:
                    if fnmatch.fnmatch(fp_name, pattern):
                        matched.append({
                            "name": fp_name,
                            "library": lib_name,
                            "lib_id": f"{lib_name}:{fp_name}",
                        })
                        seen.add(fp_name)
                        break
                if len(matched) >= 100:
                    break
            if len(matched) >= 100:
                break

        return {
            "lib_id": lib_id,
            "fp_filters": fp_filters,
            "footprints": matched,
        }


class FileLibraryManageOps(LibraryManageOps):
    """Library management (write) operations via direct file manipulation."""

    DEFAULT_EXTERNAL_LIBS_DIR = Path.home() / ".kicad-mcp" / "external_libs"

    def __init__(self, registry: LibrarySourceRegistry | None = None) -> None:
        self._registry = registry or LibrarySourceRegistry()

    def clone_library_repo(
        self, url: str, name: str, target_path: str | None = None,
    ) -> dict[str, Any]:
        dest = Path(target_path) if target_path else self.DEFAULT_EXTERNAL_LIBS_DIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and any(dest.iterdir()):
            raise GitOperationError(
                f"Target directory already exists and is not empty: {dest}",
                details={"path": str(dest)},
            )

        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(dest)],
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,  # prevent git from blocking on MCP stdio pipe
                text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=60,
            )
        except FileNotFoundError:
            raise GitOperationError(
                "git is not installed or not on PATH",
                details={"url": url},
            )
        except subprocess.CalledProcessError as exc:
            raise GitOperationError(
                f"git clone failed: {exc.stderr.strip()}",
                details={"url": url, "returncode": exc.returncode},
            )
        except subprocess.TimeoutExpired:
            raise GitOperationError(
                "git clone timed out after 60 seconds",
                details={"url": url},
            )

        self._registry.register(name, str(dest), source_type="git", url=url)
        return {
            "name": name,
            "path": str(dest),
            "url": url,
            "source_type": "git",
        }

    def register_library_source(self, path: str, name: str) -> dict[str, Any]:
        src_path = Path(path)
        if not src_path.exists():
            raise LibraryManageError(
                f"Path does not exist: {path}",
                details={"path": path},
            )
        entry = self._registry.register(name, str(src_path.resolve()), source_type="local")
        return entry

    def list_library_sources(self) -> list[dict[str, Any]]:
        return self._registry.list_all()

    def unregister_library_source(self, name: str) -> dict[str, Any]:
        source = self._registry.get(name)
        if source is None:
            raise LibraryManageError(
                f"No library source registered with name: {name}",
                details={"name": name},
            )
        self._registry.unregister(name)
        return {"name": name, "removed": True}

    def search_library_sources(
        self, query: str, source_name: str | None = None,
    ) -> dict[str, Any]:
        query_lower = query.lower()
        symbols: list[dict[str, Any]] = []
        footprints: list[dict[str, Any]] = []

        # Search symbol libraries
        for lib_path in self._registry.find_symbol_libs(source_name):
            lib_name = lib_path.stem
            try:
                tree = parse_sexp_file(lib_path)
                for node in tree:
                    if (isinstance(node, list) and len(node) >= 2
                            and node[0] == "symbol"):
                        sym_name = node[1] if isinstance(node[1], str) else ""
                        if query_lower in sym_name.lower():
                            symbols.append({
                                "name": sym_name,
                                "library": lib_name,
                                "lib_id": f"{lib_name}:{sym_name}",
                                "lib_path": str(lib_path),
                            })
            except Exception as exc:
                logger.debug("Error reading symbol lib %s: %s", lib_path, exc)

        # Search footprint libraries
        for lib_dir in self._registry.find_footprint_libs(source_name):
            lib_name = lib_dir.stem.replace(".pretty", "")
            if not lib_dir.is_dir():
                continue
            for fp_file in lib_dir.glob("*.kicad_mod"):
                if query_lower in fp_file.stem.lower():
                    footprints.append({
                        "name": fp_file.stem,
                        "library": lib_name,
                        "lib_id": f"{lib_name}:{fp_file.stem}",
                        "lib_path": str(lib_dir),
                    })

        return {
            "query": query,
            "symbols": symbols[:50],
            "footprints": footprints[:50],
        }

    def create_project_library(
        self, project_path: str, library_name: str, lib_type: str = "both",
    ) -> dict[str, Any]:
        proj_dir = Path(project_path).parent if Path(project_path).suffix else Path(project_path)
        created: list[str] = []

        if lib_type in ("symbol", "both"):
            sym_file = proj_dir / f"{library_name}.kicad_sym"
            if not sym_file.exists():
                sym_file.write_text(
                    f'(kicad_symbol_lib\n'
                    f'  (version 20231120)\n'
                    f'  (generator "kicad_mcp")\n'
                    f'  (generator_version "9.0")\n'
                    f')\n',
                    encoding="utf-8",
                )
                created.append(str(sym_file))

        if lib_type in ("footprint", "both"):
            fp_dir = proj_dir / f"{library_name}.pretty"
            if not fp_dir.exists():
                fp_dir.mkdir(parents=True, exist_ok=True)
                created.append(str(fp_dir))

        return {
            "library_name": library_name,
            "project_dir": str(proj_dir),
            "created": created,
        }

    def import_symbol(
        self, source_lib: str, symbol_name: str, target_lib_path: str,
    ) -> dict[str, Any]:
        src = Path(source_lib)
        tgt = Path(target_lib_path)

        if not src.exists():
            raise LibraryImportError(
                f"Source library not found: {source_lib}",
                details={"source_lib": source_lib},
            )
        if not tgt.exists():
            raise LibraryImportError(
                f"Target library not found: {target_lib_path}",
                details={"target_lib_path": target_lib_path},
            )

        src_content = src.read_text(encoding="utf-8")
        block = extract_sexp_block(src_content, "symbol", symbol_name)
        if block is None:
            raise LibraryImportError(
                f"Symbol '{symbol_name}' not found in {source_lib}",
                details={"symbol_name": symbol_name, "source_lib": source_lib},
            )

        tgt_content = tgt.read_text(encoding="utf-8")

        # Check if symbol already exists in target
        if extract_sexp_block(tgt_content, "symbol", symbol_name) is not None:
            raise LibraryImportError(
                f"Symbol '{symbol_name}' already exists in {target_lib_path}",
                details={"symbol_name": symbol_name, "target_lib_path": target_lib_path},
            )

        # Insert before the final closing paren
        last_paren = tgt_content.rfind(")")
        if last_paren < 0:
            raise LibraryImportError(
                f"Target file has invalid format: {target_lib_path}",
                details={"target_lib_path": target_lib_path},
            )

        tgt_content = tgt_content[:last_paren] + "  " + block + "\n" + tgt_content[last_paren:]
        tgt.write_text(tgt_content, encoding="utf-8")

        return {
            "symbol_name": symbol_name,
            "source_lib": source_lib,
            "target_lib_path": target_lib_path,
        }

    def import_footprint(
        self, source_lib: str, footprint_name: str, target_lib_path: str,
    ) -> dict[str, Any]:
        src_dir = Path(source_lib)
        tgt_dir = Path(target_lib_path)

        src_file = src_dir / f"{footprint_name}.kicad_mod"
        if not src_file.exists():
            raise LibraryImportError(
                f"Footprint file not found: {src_file}",
                details={"source_lib": source_lib, "footprint_name": footprint_name},
            )
        if not tgt_dir.exists():
            raise LibraryImportError(
                f"Target directory not found: {target_lib_path}",
                details={"target_lib_path": target_lib_path},
            )

        tgt_file = tgt_dir / f"{footprint_name}.kicad_mod"
        if tgt_file.exists():
            raise LibraryImportError(
                f"Footprint '{footprint_name}' already exists in {target_lib_path}",
                details={"footprint_name": footprint_name, "target_lib_path": target_lib_path},
            )

        shutil.copy2(str(src_file), str(tgt_file))
        return {
            "footprint_name": footprint_name,
            "source_lib": source_lib,
            "target_lib_path": target_lib_path,
            "copied_file": str(tgt_file),
        }

    def register_project_library(
        self, project_path: str, library_name: str,
        library_path: str, lib_type: str,
    ) -> dict[str, Any]:
        proj_dir = Path(project_path).parent if Path(project_path).suffix else Path(project_path)
        lib_abs = Path(library_path).resolve()

        # Compute relative path using ${KIPRJMOD}
        try:
            rel = lib_abs.relative_to(proj_dir.resolve())
            uri = "${KIPRJMOD}/" + rel.as_posix()
        except ValueError:
            uri = lib_abs.as_posix()

        if lib_type == "symbol":
            table_file = proj_dir / "sym-lib-table"
            table_tag = "sym_lib_table"
        elif lib_type == "footprint":
            table_file = proj_dir / "fp-lib-table"
            table_tag = "fp_lib_table"
        else:
            raise LibraryManageError(
                f"lib_type must be 'symbol' or 'footprint', got: {lib_type}",
                details={"lib_type": lib_type},
            )

        lib_entry = f'  (lib (name "{library_name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n'

        if table_file.exists():
            content = table_file.read_text(encoding="utf-8")
            # Check if already registered
            if f'(name "{library_name}")' in content:
                return {
                    "library_name": library_name,
                    "table_file": str(table_file),
                    "already_registered": True,
                }
            # Insert before final closing paren
            last_paren = content.rfind(")")
            if last_paren >= 0:
                content = content[:last_paren] + lib_entry + content[last_paren:]
            table_file.write_text(content, encoding="utf-8")
        else:
            content = f"({table_tag}\n{lib_entry})\n"
            table_file.write_text(content, encoding="utf-8")

        return {
            "library_name": library_name,
            "table_file": str(table_file),
            "uri": uri,
            "lib_type": lib_type,
        }


class FileBackend(KiCadBackend):
    """Pure Python file-parsing backend. Always available."""

    @property
    def name(self) -> str:
        return "file"

    @property
    def capabilities(self) -> set[BackendCapability]:
        return {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.SCHEMATIC_READ,
            BackendCapability.SCHEMATIC_MODIFY,
            BackendCapability.ERC,
            BackendCapability.LIBRARY_SEARCH,
            BackendCapability.LIBRARY_MANAGE,
        }

    def is_available(self) -> bool:
        return True

    def get_board_ops(self) -> FileBoardOps:
        return FileBoardOps()

    def get_schematic_ops(self) -> FileSchematicOps:
        return FileSchematicOps()

    def get_drc_ops(self) -> FileDRCOps:  # type: ignore[override]
        return FileDRCOps(FileSchematicOps())

    def get_library_ops(self) -> FileLibraryOps:
        return FileLibraryOps()

    def get_library_manage_ops(self) -> FileLibraryManageOps:
        return FileLibraryManageOps()


# --- S-expression parsing helpers ---

def _parse_title_block(node: list) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 2:
            if child[0] == "title":
                info["title"] = child[1]
            elif child[0] == "rev":
                info["revision"] = child[1]
            elif child[0] == "date":
                info["date"] = child[1]
    return info


def _parse_layers(node: list) -> list[str]:
    layers = []
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 3:
            layers.append(child[1])
    return layers


def _parse_footprint(node: list) -> dict[str, Any] | None:
    if len(node) < 2:
        return None
    comp: dict[str, Any] = {"footprint": node[1]}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "at" and len(child) >= 3:
            comp["position"] = {"x": float(child[1]), "y": float(child[2])}
            if len(child) >= 4:
                comp["rotation"] = float(child[3])
        elif tag == "layer":
            comp["layer"] = child[1]
        elif tag == "property" and len(child) >= 3:
            if child[1] == "Reference":
                comp["reference"] = child[2]
            elif child[1] == "Value":
                comp["value"] = child[2]
        elif tag == "fp_text" and len(child) >= 3:
            if child[1] == "reference":
                comp["reference"] = child[2]
            elif child[1] == "value":
                comp["value"] = child[2]
    return comp


def _parse_segment(node: list) -> dict[str, Any] | None:
    track: dict[str, Any] = {}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "start" and len(child) >= 3:
            track["start"] = {"x": float(child[1]), "y": float(child[2])}
        elif tag == "end" and len(child) >= 3:
            track["end"] = {"x": float(child[1]), "y": float(child[2])}
        elif tag == "width":
            track["width"] = float(child[1])
        elif tag == "layer":
            track["layer"] = child[1]
        elif tag == "net":
            track["net"] = child[1]
    return track if "start" in track else None


def _parse_setup(node: list) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "pad_to_mask_clearance":
            rules["pad_to_mask_clearance"] = float(child[1])
        elif tag == "pcbplotparams":
            continue  # Plot params are separate
        else:
            # Capture other design rule values
            try:
                rules[tag] = float(child[1])
            except (ValueError, TypeError):
                rules[tag] = child[1]
    return rules


def _parse_sheet_node(node: list) -> dict[str, Any] | None:
    """Parse a (sheet ...) S-expression block from a KiCad schematic."""
    if len(node) < 2:
        return None
    sheet: dict[str, Any] = {"pins": []}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "at" and len(child) >= 3:
            sheet["position"] = {"x": float(child[1]), "y": float(child[2])}
        elif tag == "size" and len(child) >= 3:
            sheet["size"] = {"w": float(child[1]), "h": float(child[2])}
        elif tag == "uuid":
            sheet["uuid"] = child[1]
        elif tag == "property" and len(child) >= 3:
            if child[1] == "Sheetname":
                sheet["sheetname"] = child[2]
            elif child[1] == "Sheetfile":
                sheet["sheetfile"] = child[2]
        elif tag == "pin":
            pin_info: dict[str, Any] = {}
            if len(child) >= 3:
                pin_info["name"] = child[1]
                pin_info["direction"] = child[2]
            for sub in child[1:]:
                if isinstance(sub, list) and len(sub) >= 3 and sub[0] == "at":
                    pin_info["position"] = {"x": float(sub[1]), "y": float(sub[2])}
                elif isinstance(sub, list) and len(sub) >= 2 and sub[0] == "uuid":
                    pin_info["uuid"] = sub[1]
            sheet["pins"].append(pin_info)
    return sheet if "sheetfile" in sheet else None


def _parse_sch_symbol(node: list) -> dict[str, Any] | None:
    if len(node) < 2:
        return None
    sym: dict[str, Any] = {}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "lib_id":
            sym["lib_id"] = child[1]
            sym["is_power"] = child[1].startswith("power:")
        elif tag == "at" and len(child) >= 3:
            sym["position"] = {"x": float(child[1]), "y": float(child[2])}
        elif tag == "property" and len(child) >= 3:
            if child[1] == "Reference":
                sym["reference"] = child[2]
            elif child[1] == "Value":
                sym["value"] = child[2]
            elif child[1] == "Footprint":
                sym["footprint"] = child[2]
    return sym if sym else None


def _parse_sch_wire(node: list) -> dict[str, Any] | None:
    for child in node[1:]:
        if isinstance(child, list) and len(child) > 0 and child[0] == "pts":
            points = []
            for pt in child[1:]:
                if isinstance(pt, list) and len(pt) >= 3 and pt[0] == "xy":
                    points.append({"x": float(pt[1]), "y": float(pt[2])})
            if len(points) >= 2:
                return {"start": points[0], "end": points[1]}
    return None


def _parse_position_node(node: list) -> dict[str, Any] | None:
    """Parse a node that has an (at x y) child, returning {"position": {"x": ..., "y": ...}}."""
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 3 and child[0] == "at":
            return {"position": {"x": float(child[1]), "y": float(child[2])}}
    return None


def _parse_sch_label(node: list, label_type: str) -> dict[str, Any] | None:
    if len(node) < 2:
        return None
    label: dict[str, Any] = {"label_type": label_type, "text": node[1]}
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 3 and child[0] == "at":
            label["position"] = {"x": float(child[1]), "y": float(child[2])}
    return label


def _parse_pin_node(child: list) -> dict[str, Any]:
    """Parse a single pin s-expression node and extract type, shape, name, number, position."""
    pin_info: dict[str, Any] = {}
    if len(child) >= 3:
        pin_info["type"] = child[1]
        pin_info["shape"] = child[2]
    for sub in child[1:]:
        if not isinstance(sub, list) or len(sub) < 2:
            continue
        if sub[0] == "name":
            pin_info["name"] = sub[1]
        elif sub[0] == "number":
            pin_info["number"] = sub[1]
        elif sub[0] == "at" and len(sub) >= 3:
            pos: dict[str, Any] = {"x": float(sub[1]), "y": float(sub[2])}
            if len(sub) >= 4:
                pos["angle"] = float(sub[3])
            else:
                pos["angle"] = 0.0
            pin_info["position"] = pos
    return pin_info


def _parse_symbol_detail(node: list, lib_name: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": node[1] if len(node) > 1 else "",
        "library": lib_name,
        "pins": [],
    }
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "property" and len(child) >= 3:
            prop_name = child[1]
            prop_val = child[2]
            if prop_name == "ki_description":
                info["description"] = prop_val
            elif prop_name == "ki_keywords":
                info["keywords"] = prop_val
            elif prop_name == "ki_fp_filters":
                info["fp_filters"] = prop_val.split() if isinstance(prop_val, str) else []
            elif prop_name == "Datasheet":
                info["datasheet"] = prop_val
        elif tag == "pin":
            info["pins"].append(_parse_pin_node(child))
        elif tag == "symbol":
            # Recurse into sub-symbols (e.g. "SCD41_1_1") to find pins
            for sub_child in child[1:]:
                if isinstance(sub_child, list) and len(sub_child) >= 2 and sub_child[0] == "pin":
                    info["pins"].append(_parse_pin_node(sub_child))
    info["pin_count"] = len(info["pins"])
    return info


def _parse_footprint_detail(tree: list, lib_name: str, fp_name: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": fp_name,
        "library": lib_name,
        "pads": [],
    }
    for node in tree:
        if not isinstance(node, list) or len(node) < 2:
            continue
        tag = node[0] if isinstance(node[0], str) else ""
        if tag == "descr":
            info["description"] = node[1]
        elif tag == "tags":
            info["keywords"] = node[1]
        elif tag == "pad":
            pad_info: dict[str, Any] = {}
            if len(node) >= 3:
                pad_info["number"] = node[1]
                pad_info["type"] = node[2]
            if len(node) >= 4:
                pad_info["shape"] = node[3]
            info["pads"].append(pad_info)
    info["pad_count"] = len(info["pads"])
    info["smd"] = any(p.get("type") == "smd" for p in info["pads"])
    return info
