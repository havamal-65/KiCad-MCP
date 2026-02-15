"""Pure Python file-parsing backend - always available, no KiCad installation needed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    KiCadBackend,
    LibraryOps,
    SchematicOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.sexp_parser import parse_sexp_file

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
            if hasattr(sym, "lib_id"):
                symbol_data["lib_id"] = str(sym.lib_id)
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

        return {
            "info": {
                "file_path": str(path),
                "num_symbols": len(symbols),
                "num_wires": len(wires),
                "num_labels": len(labels),
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
        }

    def _read_with_sexp(self, path: Path) -> dict[str, Any]:
        tree = parse_sexp_file(path)
        symbols = []
        wires = []
        labels = []

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

        return {
            "info": {
                "file_path": str(path),
                "num_symbols": len(symbols),
                "num_wires": len(wires),
                "num_labels": len(labels),
            },
            "symbols": symbols,
            "wires": wires,
            "labels": labels,
        }

    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        result = self.read_schematic(path)
        return result.get("symbols", [])

    def add_component(
        self, path: Path, lib_id: str, reference: str, value: str,
        x: float, y: float,
    ) -> dict[str, Any]:
        try:
            from skip import Schematic
        except ImportError:
            raise NotImplementedError(
                "Schematic modification requires kicad-skip. Install: pip install kicad-skip"
            )

        sch = Schematic(str(path))
        # Use kicad-skip to add symbol
        import uuid
        symbol_uuid = str(uuid.uuid4())

        # Build the symbol s-expression and append
        sym_sexp = (
            f'  (symbol (lib_id "{lib_id}") (at {x} {y} 0)\n'
            f'    (uuid "{symbol_uuid}")\n'
            f'    (property "Reference" "{reference}" (at {x} {y - 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
            f'    (property "Value" "{value}" (at {x} {y + 2} 0)\n'
            f'      (effects (font (size 1.27 1.27)))\n'
            f'    )\n'
            f'  )\n'
        )

        # Read file and insert before closing paren
        content = path.read_text(encoding="utf-8")
        last_paren = content.rfind(")")
        if last_paren >= 0:
            content = content[:last_paren] + sym_sexp + content[last_paren:]
            path.write_text(content, encoding="utf-8")

        return {
            "reference": reference,
            "value": value,
            "lib_id": lib_id,
            "position": {"x": x, "y": y},
            "uuid": symbol_uuid,
        }

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
        }

    def is_available(self) -> bool:
        return True

    def get_board_ops(self) -> FileBoardOps:
        return FileBoardOps()

    def get_schematic_ops(self) -> FileSchematicOps:
        return FileSchematicOps()

    def get_library_ops(self) -> FileLibraryOps:
        return FileLibraryOps()


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
        elif tag == "at" and len(child) >= 3:
            sym["position"] = {"x": float(child[1]), "y": float(child[2])}
        elif tag == "property" and len(child) >= 3:
            if child[1] == "Reference":
                sym["reference"] = child[2]
            elif child[1] == "Value":
                sym["value"] = child[2]
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


def _parse_sch_label(node: list, label_type: str) -> dict[str, Any] | None:
    if len(node) < 2:
        return None
    label: dict[str, Any] = {"label_type": label_type, "text": node[1]}
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 3 and child[0] == "at":
            label["position"] = {"x": float(child[1]), "y": float(child[2])}
    return label


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
            pin_info: dict[str, Any] = {}
            if len(child) >= 3:
                pin_info["type"] = child[1]
                pin_info["shape"] = child[2]
            for sub in child[1:]:
                if isinstance(sub, list) and sub[0] == "name" and len(sub) >= 2:
                    pin_info["name"] = sub[1]
                elif isinstance(sub, list) and sub[0] == "number" and len(sub) >= 2:
                    pin_info["number"] = sub[1]
            info["pins"].append(pin_info)
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
