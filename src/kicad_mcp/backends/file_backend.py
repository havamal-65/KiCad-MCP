"""Pure Python file-parsing backend - always available, no KiCad installation needed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    KiCadBackend,
    LibraryManageOps,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.models.errors import GitOperationError, LibraryImportError, LibraryManageError
from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.sexp_parser import (
    extract_sexp_block,
    find_symbol_block_by_reference,
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


class FileSchematicOps(SchematicOps):
    """Read-only schematic operations via kicad-skip or direct parsing."""

    def read_schematic(self, path: Path) -> dict[str, Any]:
        try:
            from skip import Schematic
            sch = Schematic(str(path))
            return self._read_with_skip(sch, path)
        except ImportError:
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
                if hasattr(lbl, "text"):
                    label_data["text"] = str(lbl.text)
                elif hasattr(lbl, "name"):
                    label_data["text"] = str(lbl.name)
                if hasattr(lbl, "at"):
                    pos = self._skip_at_to_pos(lbl.at)
                    if pos:
                        label_data["position"] = pos
                labels.append(label_data)

        # Parse no_connects and junctions via sexp fallback (skip doesn't expose these well)
        no_connects = []
        junctions = []
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
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
            "no_connects": no_connects,
            "junctions": junctions,
        }

    def _read_with_sexp(self, path: Path) -> dict[str, Any]:
        tree = parse_sexp_file(path)
        symbols = []
        wires = []
        labels = []
        no_connects = []
        junctions = []

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

        return {
            "info": {
                "file_path": str(path),
                "num_symbols": len(symbols),
                "num_wires": len(wires),
                "num_labels": len(labels),
                "num_no_connects": len(no_connects),
                "num_junctions": len(junctions),
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
            "no_connects": no_connects,
            "junctions": junctions,
        }

    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        result = self.read_schematic(path)
        return result.get("symbols", [])

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

        sym_sexp = (
            f'  (symbol (lib_id "{lib_id}") {at_clause}{mirror_clause}\n'
            f'    (uuid "{symbol_uuid}")\n'
            f'{prop_lines}'
            f'  )\n'
        )

        content = path.read_text(encoding="utf-8")
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + sym_sexp + content[last_paren:]
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
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + wire_sexp + content[last_paren:]
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
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + label_sexp + content[last_paren:]
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
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + nc_sexp + content[last_paren:]
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

        sym_sexp = (
            f'  (symbol (lib_id "{lib_id}") (at {x} {y} {rotation})\n'
            f'    (uuid "{symbol_uuid}")\n'
            f'    (property "Reference" "{pwr_ref}" (at {x} {y - 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)) hide)\n'
            f'    )\n'
            f'    (property "Value" "{name}" (at {x} {y + 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
            f'  )\n'
        )

        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + sym_sexp + content[last_paren:]
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
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + jn_sexp + content[last_paren:]
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

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in {path}")
        start, end = location
        content = remove_sexp_block(content, start, end)
        path.write_text(content, encoding="utf-8")
        return {"reference": reference, "removed": True}

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
            return {"error": f"Library symbol '{lib_id}' not found in lib_symbols cache"}

        # 3. Extract pins from lib symbol (recurse into sub-symbols)
        pins = []
        for child in lib_sym[1:]:
            if not isinstance(child, list) or len(child) < 2:
                continue
            if child[0] == "pin":
                pins.append(_parse_pin_node(child))
            elif child[0] == "symbol":
                for sub_child in child[1:]:
                    if isinstance(sub_child, list) and len(sub_child) >= 2 and sub_child[0] == "pin":
                        pins.append(_parse_pin_node(sub_child))

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
                text=True,
                timeout=300,
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
                "git clone timed out after 300 seconds",
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
            BackendCapability.SCHEMATIC_READ,
            BackendCapability.SCHEMATIC_MODIFY,
            BackendCapability.LIBRARY_SEARCH,
            BackendCapability.LIBRARY_MANAGE,
        }

    def is_available(self) -> bool:
        return True

    def get_board_ops(self) -> FileBoardOps:
        return FileBoardOps()

    def get_schematic_ops(self) -> FileSchematicOps:
        return FileSchematicOps()

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
