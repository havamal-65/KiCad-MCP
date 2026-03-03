"""IPC backend for KiCad 9+ via kipy (kicad-python)."""

from __future__ import annotations

import re
import uuid as _uuid
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

logger = get_logger("backend.ipc")

# Lazy import - kipy may not be available
_kipy = None
_kicad = None


def _get_kicad():
    global _kipy, _kicad
    if _kicad is None:
        import kipy
        _kipy = kipy
        _kicad = kipy.KiCad()
    return _kicad


def _reset_kicad() -> None:
    """Discard the cached KiCad connection so the next call reconnects."""
    global _kicad
    _kicad = None


def _is_token_error(exc: Exception) -> bool:
    return "token" in str(exc).lower()


class IPCBoardOps(BoardOps):
    """Board operations via KiCad 9+ IPC API."""

    def __init__(self) -> None:
        from kicad_mcp.backends.file_backend import FileBoardOps
        self._file_ops = FileBoardOps()

    def read_board(self, path: Path) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            kicad.get_board()  # probe — raises if no board open / IPC unavailable
            info = self._get_board_info_ipc()
            components = self._get_components_ipc()
            nets = self._get_nets_ipc()
            tracks = self._get_tracks_ipc()
            return {"info": info, "components": components, "nets": nets, "tracks": tracks}
        except Exception as e:
            logger.debug("IPC read_board failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.read_board(path)

    def get_board_info(self, path: Path) -> dict[str, Any]:
        try:
            return self._get_board_info_ipc()
        except Exception as e:
            logger.debug("IPC get_board_info failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.get_board_info(path)

    def _get_board_info_ipc(self) -> dict[str, Any]:
        kicad = _get_kicad()
        board = kicad.get_board()
        return {
            "title": board.get_title() if hasattr(board, "get_title") else "",
            "num_components": len(board.get_footprints()) if hasattr(board, "get_footprints") else 0,
            "num_nets": len(board.get_nets()) if hasattr(board, "get_nets") else 0,
            "num_tracks": len(board.get_tracks()) if hasattr(board, "get_tracks") else 0,
        }

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        try:
            return self._get_components_ipc()
        except Exception as e:
            logger.debug("IPC get_components failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.get_components(path)

    def _get_components_ipc(self) -> list[dict[str, Any]]:
        kicad = _get_kicad()
        board = kicad.get_board()
        components = []
        if hasattr(board, "get_footprints"):
            for fp in board.get_footprints():
                comp: dict[str, Any] = {
                    "reference": fp.reference if hasattr(fp, "reference") else "",
                    "value": fp.value if hasattr(fp, "value") else "",
                }
                if hasattr(fp, "position"):
                    comp["position"] = {"x": fp.position.x, "y": fp.position.y}
                if hasattr(fp, "layer"):
                    comp["layer"] = fp.layer
                components.append(comp)
        return components

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        try:
            return self._get_nets_ipc()
        except Exception as e:
            logger.debug("IPC get_nets failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.get_nets(path)

    def _get_nets_ipc(self) -> list[dict[str, Any]]:
        kicad = _get_kicad()
        board = kicad.get_board()
        nets = []
        if hasattr(board, "get_nets"):
            for net in board.get_nets():
                nets.append({
                    "name": net.name if hasattr(net, "name") else str(net),
                    "number": net.number if hasattr(net, "number") else 0,
                })
        return nets

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        try:
            return self._get_tracks_ipc()
        except Exception as e:
            logger.debug("IPC get_tracks failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.get_tracks(path)

    def _get_tracks_ipc(self) -> list[dict[str, Any]]:
        kicad = _get_kicad()
        board = kicad.get_board()
        tracks = []
        if hasattr(board, "get_tracks"):
            for track in board.get_tracks():
                track_data: dict[str, Any] = {}
                if hasattr(track, "start"):
                    track_data["start"] = {"x": track.start.x, "y": track.start.y}
                if hasattr(track, "end"):
                    track_data["end"] = {"x": track.end.x, "y": track.end.y}
                if hasattr(track, "width"):
                    track_data["width"] = track.width
                if hasattr(track, "layer"):
                    track_data["layer"] = track.layer
                tracks.append(track_data)
        return tracks

    # -- IPC infrastructure (board) --------------------------------------

    def _get_board_doc(self):
        """Get the DocumentSpecifier for the open PCB."""
        from kipy.proto.common.types import DocumentType
        kicad = _get_kicad()
        docs = kicad.get_open_documents(DocumentType.DOCTYPE_PCB)
        if not docs:
            raise RuntimeError("No board is open in KiCad")
        return docs[0]

    def _get_client(self):
        """Get the KiCadClient for sending raw commands."""
        return _get_kicad()._client

    def _parse_and_create(self, sexp: str) -> dict[str, Any]:
        """Send an S-expression string to KiCad to create board items."""
        from kipy.proto.common.commands.editor_commands_pb2 import (
            CreateItemsResponse, ParseAndCreateItemsFromString,
        )
        doc = self._get_board_doc()
        command = ParseAndCreateItemsFromString()
        command.document.CopyFrom(doc)
        command.contents = sexp
        response = self._get_client().send(command, CreateItemsResponse)
        return {"status": "ok", "created_count": len(response.created_items)}

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            board = kicad.get_board()

            if hasattr(board, "place_footprint"):
                board.place_footprint(
                    footprint=footprint,
                    reference=reference,
                    position=(x, y),
                    layer=layer,
                    rotation=rotation,
                )
                return {
                    "reference": reference,
                    "footprint": footprint,
                    "position": {"x": x, "y": y},
                }

            # Fallback: load .kicad_mod and inject via ParseAndCreateItemsFromString
            from kicad_mcp.backends.file_backend import (
                _embed_kicad_mod_as_pcb_footprint,
                _load_kicad_mod,
            )
            import uuid as _uuid_mod
            mod_text = _load_kicad_mod(footprint)
            if mod_text is None:
                raise ValueError(f"Footprint not found: {footprint}")
            fp_uuid = str(_uuid_mod.uuid4())
            fp_block = _embed_kicad_mod_as_pcb_footprint(
                mod_text, footprint, reference, x, y, layer, rotation, fp_uuid
            )
            self._parse_and_create(fp_block)
            return {
                "reference": reference,
                "footprint": footprint,
                "position": {"x": x, "y": y},
                "layer": layer,
                "rotation": rotation,
            }
        except Exception as e:
            if _is_token_error(e):
                _reset_kicad()
            logger.warning("IPC place_component failed (%s), falling back to file backend", e)
            from kicad_mcp.backends.file_backend import FileBoardOps
            return FileBoardOps().place_component(path, reference, footprint, x, y, layer, rotation)

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            board = kicad.get_board()

            if hasattr(board, "get_footprints"):
                for fp in board.get_footprints():
                    ref = fp.reference if hasattr(fp, "reference") else ""
                    if ref == reference:
                        if hasattr(fp, "set_position"):
                            fp.set_position(x, y)
                        if rotation is not None and hasattr(fp, "set_rotation"):
                            fp.set_rotation(rotation)
                        return {
                            "reference": reference,
                            "position": {"x": x, "y": y},
                        }
        except Exception as e:
            if _is_token_error(e):
                _reset_kicad()
            logger.warning("IPC move_component failed (%s), falling back to file backend", e)
            from kicad_mcp.backends.file_backend import FileBoardOps
            return FileBoardOps().move_component(path, reference, x, y, rotation)

        raise ValueError(f"Component {reference} not found")

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        """Assign a net to a component pad (file-backend implementation with IPC not needed)."""
        try:
            from kicad_mcp.backends.file_backend import FileBoardOps
            return FileBoardOps().assign_net(path, reference, pad, net)
        except Exception as e:
            logger.warning("assign_net failed: %s", e)
            raise

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            board = kicad.get_board()

            if hasattr(board, "add_track"):
                board.add_track(
                    start=(start_x, start_y),
                    end=(end_x, end_y),
                    width=width,
                    layer=layer,
                    net=net,
                )
                return {
                    "start": {"x": start_x, "y": start_y},
                    "end": {"x": end_x, "y": end_y},
                    "width": width,
                    "layer": layer,
                }

            # Fallback: inject segment via ParseAndCreateItemsFromString
            seg_uuid = str(_uuid.uuid4())
            sexp = (
                f'(segment (start {start_x} {start_y}) (end {end_x} {end_y}) '
                f'(width {width}) (layer "{layer}") (net 0) '
                f'(uuid "{seg_uuid}"))'
            )
            self._parse_and_create(sexp)
            return {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
                "width": width,
                "layer": layer,
            }
        except Exception as e:
            if _is_token_error(e):
                _reset_kicad()
            logger.warning("IPC add_track failed (%s), falling back to file backend", e)
            from kicad_mcp.backends.file_backend import FileBoardOps
            return FileBoardOps().add_track(path, start_x, start_y, end_x, end_y, width, layer, net)

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            board = kicad.get_board()

            if hasattr(board, "add_via"):
                board.add_via(
                    position=(x, y),
                    size=size,
                    drill=drill,
                    net=net,
                )
                return {
                    "position": {"x": x, "y": y},
                    "size": size,
                    "drill": drill,
                }

            # Fallback: inject via via ParseAndCreateItemsFromString
            via_uuid = str(_uuid.uuid4())
            sexp = (
                f'(via (at {x} {y}) (size {size}) (drill {drill}) '
                f'(layers "F.Cu" "B.Cu") (net 0) (uuid "{via_uuid}"))'
            )
            self._parse_and_create(sexp)
            return {
                "position": {"x": x, "y": y},
                "size": size,
                "drill": drill,
            }
        except Exception as e:
            if _is_token_error(e):
                _reset_kicad()
            logger.warning("IPC add_via failed (%s), falling back to file backend", e)
            from kicad_mcp.backends.file_backend import FileBoardOps
            return FileBoardOps().add_via(path, x, y, size, drill, net, via_type)

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        try:
            kicad = _get_kicad()
            board = kicad.get_board()
            if hasattr(board, "get_design_settings"):
                ds = board.get_design_settings()
                return {
                    "min_track_width": getattr(ds, "min_track_width", None),
                    "min_via_diameter": getattr(ds, "min_via_diameter", None),
                    "min_via_drill": getattr(ds, "min_via_drill", None),
                    "min_clearance": getattr(ds, "min_clearance", None),
                }
            return {}
        except Exception as e:
            logger.debug("IPC get_design_rules failed (%s), returning empty dict", e)
            if _is_token_error(e):
                _reset_kicad()
            return {}

    def refill_zones(self, path: Path) -> dict[str, Any]:
        """Refill all copper pour zones on the board."""
        try:
            kicad = _get_kicad()
            board = kicad.get_board()
            if hasattr(board, "refill_zones"):
                board.refill_zones()
                if hasattr(board, "save"):
                    board.save()
                return {"status": "success", "message": "All copper zones refilled"}
            return {"status": "unavailable", "reason": "IPC board does not support refill_zones"}
        except Exception as e:
            logger.warning("IPC refill_zones failed: %s", e)
            if _is_token_error(e):
                _reset_kicad()
            return {"status": "error", "message": f"Zone refill failed: {e}"}

    def get_stackup(self, path: Path) -> dict[str, Any]:
        """Return the board layer stackup."""
        try:
            kicad = _get_kicad()
            board = kicad.get_board()
            if not hasattr(board, "get_stackup"):
                return {"status": "unavailable", "reason": "IPC board does not support get_stackup"}
            stackup = board.get_stackup()
            result: dict[str, Any] = {}
            for attr in ("board_thickness", "castellation_pads", "edge_connector",
                         "edge_plating", "finish", "impedance_controlled"):
                val = getattr(stackup, attr, None)
                if val is not None:
                    result[attr] = val
            layers: list[dict[str, Any]] = []
            if hasattr(stackup, "layers"):
                for layer in stackup.layers:
                    layer_dict: dict[str, Any] = {}
                    for field in ("name", "type", "material", "thickness",
                                  "dielectric_constant", "loss_tangent", "color"):
                        v = getattr(layer, field, None)
                        if v is not None:
                            layer_dict[field] = v
                    layers.append(layer_dict)
            result["layers"] = layers
            return {"status": "success", "stackup": result}
        except Exception as e:
            logger.debug("IPC get_stackup failed (%s)", e)
            if _is_token_error(e):
                _reset_kicad()
            return {"status": "unavailable", "reason": f"Stackup query failed: {e}"}


class IPCLibraryOps(LibraryOps):
    """Library operations via KiCad 9+ IPC API."""

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        try:
            kicad = _get_kicad()
            results = []
            if hasattr(kicad, "get_symbol_libraries"):
                for lib in kicad.get_symbol_libraries():
                    if hasattr(lib, "get_symbols"):
                        for sym in lib.get_symbols():
                            sym_name = sym.name if hasattr(sym, "name") else str(sym)
                            if query.lower() in sym_name.lower():
                                results.append({
                                    "name": sym_name,
                                    "library": lib.name if hasattr(lib, "name") else "",
                                    "lib_id": f"{lib.name}:{sym_name}" if hasattr(lib, "name") else sym_name,
                                })
            return results[:50]
        except Exception as e:
            logger.debug("IPC search_symbols failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            from kicad_mcp.backends.file_backend import FileLibraryOps
            return FileLibraryOps().search_symbols(query)

    def search_footprints(self, query: str) -> list[dict[str, Any]]:
        try:
            kicad = _get_kicad()
            results = []
            if hasattr(kicad, "get_footprint_libraries"):
                for lib in kicad.get_footprint_libraries():
                    if hasattr(lib, "get_footprints"):
                        for fp in lib.get_footprints():
                            fp_name = fp.name if hasattr(fp, "name") else str(fp)
                            if query.lower() in fp_name.lower():
                                results.append({
                                    "name": fp_name,
                                    "library": lib.name if hasattr(lib, "name") else "",
                                    "lib_id": f"{lib.name}:{fp_name}" if hasattr(lib, "name") else fp_name,
                                })
            return results[:50]
        except Exception as e:
            logger.debug("IPC search_footprints failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            from kicad_mcp.backends.file_backend import FileLibraryOps
            return FileLibraryOps().search_footprints(query)

    def list_libraries(self) -> list[dict[str, Any]]:
        try:
            kicad = _get_kicad()
            libs = []
            if hasattr(kicad, "get_symbol_libraries"):
                for lib in kicad.get_symbol_libraries():
                    libs.append({
                        "name": lib.name if hasattr(lib, "name") else str(lib),
                        "type": "symbol",
                    })
            if hasattr(kicad, "get_footprint_libraries"):
                for lib in kicad.get_footprint_libraries():
                    libs.append({
                        "name": lib.name if hasattr(lib, "name") else str(lib),
                        "type": "footprint",
                    })
            return libs
        except Exception as e:
            logger.debug("IPC list_libraries failed (%s), falling back to file backend", e)
            if _is_token_error(e):
                _reset_kicad()
            from kicad_mcp.backends.file_backend import FileLibraryOps
            return FileLibraryOps().list_libraries()


class IPCSchematicOps(SchematicOps):
    """Schematic operations via KiCad 9+ IPC API.

    Uses ParseAndCreateItemsFromString for create operations (S-expression passthrough)
    and SaveDocumentToString + DeleteItems for removal operations.
    Read operations delegate to the file backend after flushing KiCad's state to disk.
    """

    def __init__(self, file_schematic_ops: SchematicOps) -> None:
        self._file_ops = file_schematic_ops

    # -- IPC infrastructure -----------------------------------------------

    def _get_schematic_doc(self):
        """Get the DocumentSpecifier for the open schematic."""
        from kipy.proto.common.types import DocumentType
        try:
            kicad = _get_kicad()
            docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        except Exception as e:
            if _is_token_error(e):
                logger.debug("Token error on _get_schematic_doc, resetting connection")
                _reset_kicad()
                kicad = _get_kicad()
                docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
            else:
                raise
        if not docs:
            raise RuntimeError("No schematic is open in KiCad")
        return docs[0]

    def _get_client(self):
        """Get the KiCadClient for sending raw commands."""
        return _get_kicad()._client

    def _try_save_to_disk(self) -> bool:
        """Try to flush KiCad's schematic state to disk.

        Returns True if successful.  Returns False on any failure (no schematic
        open, gRPC timeout, token error, etc.) — the file on disk is then already
        current from prior file-backend writes, so callers can proceed with file
        reads directly.  Never re-raises.
        """
        try:
            self._save_to_disk()
            return True
        except Exception as e:
            if "No schematic is open" not in str(e):
                logger.debug("IPC save-to-disk failed (%s); file already current", e)
            return False

    def _save_to_disk(self):
        """Flush KiCad's in-memory schematic state to disk."""
        from google.protobuf.empty_pb2 import Empty
        from kipy.proto.common.commands.editor_commands_pb2 import SaveDocument
        doc = self._get_schematic_doc()
        command = SaveDocument()
        command.document.CopyFrom(doc)
        self._get_client().send(command, Empty)

    def _get_doc_as_string(self) -> str:
        """Get the schematic document as an S-expression string."""
        from kipy.proto.common.commands.editor_commands_pb2 import (
            SaveDocumentToString, SavedDocumentResponse,
        )
        doc = self._get_schematic_doc()
        command = SaveDocumentToString()
        command.document.CopyFrom(doc)
        return self._get_client().send(command, SavedDocumentResponse).contents

    def _parse_and_create(self, sexp: str) -> dict[str, Any]:
        """Send an S-expression string to KiCad to create items."""
        from kipy.proto.common.commands.editor_commands_pb2 import (
            CreateItemsResponse, ParseAndCreateItemsFromString,
        )
        doc = self._get_schematic_doc()
        command = ParseAndCreateItemsFromString()
        command.document.CopyFrom(doc)
        command.contents = sexp
        response = self._get_client().send(command, CreateItemsResponse)
        return {"status": "ok", "created_count": len(response.created_items)}

    def _delete_by_uuid(self, uuid_str: str) -> None:
        """Delete an item from the schematic by its UUID string."""
        from kipy.proto.common.commands.editor_commands_pb2 import (
            DeleteItems, DeleteItemsResponse,
        )
        from kipy.proto.common.types.base_types_pb2 import KIID
        doc = self._get_schematic_doc()
        command = DeleteItems()
        command.header.document.CopyFrom(doc)
        kiid = KIID()
        kiid.value = uuid_str
        command.item_ids.append(kiid)
        self._get_client().send(command, DeleteItemsResponse)

    def _find_uuid_in_sexp(self, content: str, block_start: int, block_end: int) -> str | None:
        """Extract the UUID from an S-expression block."""
        block = content[block_start:block_end + 1]
        m = re.search(r'\(uuid\s+"([^"]+)"\)', block)
        return m.group(1) if m else None

    # -- Read operations (delegate to file backend after flush) -----------

    def read_schematic(self, path: Path) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.read_schematic(path)

    def get_symbols(self, path: Path) -> list[dict[str, Any]]:
        self._try_save_to_disk()
        return self._file_ops.get_symbols(path)

    def get_symbol_pin_positions(self, path: Path, reference: str) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.get_symbol_pin_positions(path, reference)

    def get_sheet_hierarchy(self, path: Path) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.get_sheet_hierarchy(path)

    def get_pin_net(self, path: Path, reference: str, pin_number: str) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.get_pin_net(path, reference, pin_number)

    def get_net_connections(self, path: Path, net_name: str) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.get_net_connections(path, net_name)

    def validate_schematic(self, path: Path) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.validate_schematic(path)

    # -- Create operations (via ParseAndCreateItemsFromString) ------------

    def add_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        try:
            wire_uuid = str(_uuid.uuid4())
            sexp = (
                f'(wire (pts (xy {start_x} {start_y}) (xy {end_x} {end_y}))\n'
                f'  (stroke (width 0) (type default))\n'
                f'  (uuid "{wire_uuid}")\n'
                f')'
            )
            self._parse_and_create(sexp)
            return {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
                "uuid": wire_uuid,
            }
        except Exception as e:
            logger.warning("IPC add_wire failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_wire(path, start_x, start_y, end_x, end_y)

    def add_label(
        self, path: Path, text: str, x: float, y: float,
        label_type: str = "net_label",
    ) -> dict[str, Any]:
        try:
            label_uuid = str(_uuid.uuid4())
            tag = label_type if label_type != "net_label" else "label"
            sexp = (
                f'({tag} "{text}" (at {x} {y} 0)\n'
                f'  (effects (font (size 1.27 1.27)))\n'
                f'  (uuid "{label_uuid}")\n'
                f')'
            )
            self._parse_and_create(sexp)
            return {
                "text": text,
                "position": {"x": x, "y": y},
                "label_type": label_type,
                "uuid": label_uuid,
            }
        except Exception as e:
            logger.warning("IPC add_label failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_label(path, text, x, y, label_type)

    def add_component(
        self, path: Path, lib_id: str, reference: str, value: str,
        x: float, y: float, rotation: float = 0.0,
        mirror: str | None = None, footprint: str = "",
        properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._add_component_via_ipc(
                path, lib_id, reference, value, x, y, rotation, mirror, footprint, properties,
            )
        except Exception as e:
            logger.warning("IPC add_component failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_component(
                path, lib_id, reference, value, x, y, rotation, mirror, footprint, properties,
            )

    def _add_component_via_ipc(
        self, path: Path, lib_id: str, reference: str, value: str,
        x: float, y: float, rotation: float = 0.0,
        mirror: str | None = None, footprint: str = "",
        properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        symbol_uuid = str(_uuid.uuid4())

        # Build at clause
        at_clause = f"(at {x} {y} {rotation})"

        # Build mirror clause
        mirror_clause = ""
        if mirror in ("x", "y"):
            mirror_clause = f"\n  (mirror {mirror})"

        # Build properties
        prop_lines = (
            f'  (property "Reference" "{reference}" (at {x} {y - 2} 0)\n'
            f'    (effects (font (size 1.27 1.27)))\n'
            f'  )\n'
            f'  (property "Value" "{value}" (at {x} {y + 2} 0)\n'
            f'    (effects (font (size 1.27 1.27)))\n'
            f'  )\n'
        )

        if footprint:
            prop_lines += (
                f'  (property "Footprint" "{footprint}" (at {x} {y + 4} 0)\n'
                f'    (effects (font (size 1.27 1.27)) hide)\n'
                f'  )\n'
            )

        if properties:
            offset = 6
            for prop_name, prop_val in properties.items():
                prop_lines += (
                    f'  (property "{prop_name}" "{prop_val}" (at {x} {y + offset} 0)\n'
                    f'    (effects (font (size 1.27 1.27)) hide)\n'
                    f'  )\n'
                )
                offset += 2

        # Ensure lib_symbols cache is up-to-date for this symbol.
        # Save to disk first, then use file ops to inject the lib_symbol cache,
        # then reload in KiCad before pasting the symbol instance.
        self._save_to_disk()
        content = path.read_text(encoding="utf-8")
        updated_content = self._file_ops._ensure_lib_symbol_cached(content, lib_id)
        if updated_content != content:
            path.write_text(updated_content, encoding="utf-8")
            # Reload in KiCad so the lib_symbols are available
            from kipy.proto.common.commands.editor_commands_pb2 import RevertDocument
            from google.protobuf.empty_pb2 import Empty
            doc = self._get_schematic_doc()
            command = RevertDocument()
            command.document.CopyFrom(doc)
            self._get_client().send(command, Empty)

        sch_uuid = self._file_ops._find_schematic_uuid(
            self._get_doc_as_string()
        )

        sym_sexp = (
            f'(symbol (lib_id "{lib_id}") {at_clause}{mirror_clause} (unit 1)\n'
            f'  (in_bom yes) (on_board yes) (dnp no)\n'
            f'  (uuid "{symbol_uuid}")\n'
            f'{prop_lines}'
            f'  (instances\n'
            f'    (project ""\n'
            f'      (path "/{sch_uuid}"\n'
            f'        (reference "{reference}") (unit 1)\n'
            f'      )\n'
            f'    )\n'
            f'  )\n'
            f')'
        )
        self._parse_and_create(sym_sexp)

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

    def add_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        try:
            nc_uuid = str(_uuid.uuid4())
            sexp = f'(no_connect (at {x} {y}) (uuid "{nc_uuid}"))'
            self._parse_and_create(sexp)
            return {
                "position": {"x": x, "y": y},
                "uuid": nc_uuid,
            }
        except Exception as e:
            logger.warning("IPC add_no_connect failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_no_connect(path, x, y)

    def add_power_symbol(
        self, path: Path, name: str, x: float, y: float, rotation: float = 0.0,
    ) -> dict[str, Any]:
        try:
            return self._add_power_symbol_via_ipc(path, name, x, y, rotation)
        except Exception as e:
            logger.warning("IPC add_power_symbol failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_power_symbol(path, name, x, y, rotation)

    def _add_power_symbol_via_ipc(
        self, path: Path, name: str, x: float, y: float, rotation: float = 0.0,
    ) -> dict[str, Any]:
        symbol_uuid = str(_uuid.uuid4())
        lib_id = f"power:{name}"

        # Auto-increment PWR reference by scanning the current document
        doc_content = self._get_doc_as_string()
        pwr_refs = re.findall(r'"#PWR(\d+)"', doc_content)
        next_num = max((int(n) for n in pwr_refs), default=0) + 1
        pwr_ref = f"#PWR{next_num:03d}"

        # Ensure lib_symbols cache has this power symbol
        self._save_to_disk()
        content = path.read_text(encoding="utf-8")
        updated_content = self._file_ops._ensure_lib_symbol_cached(content, lib_id)
        if updated_content != content:
            path.write_text(updated_content, encoding="utf-8")
            from kipy.proto.common.commands.editor_commands_pb2 import RevertDocument
            from google.protobuf.empty_pb2 import Empty
            doc = self._get_schematic_doc()
            command = RevertDocument()
            command.document.CopyFrom(doc)
            self._get_client().send(command, Empty)

        sch_uuid = self._file_ops._find_schematic_uuid(
            self._get_doc_as_string()
        )

        sym_sexp = (
            f'(symbol (lib_id "{lib_id}") (at {x} {y} {rotation}) (unit 1)\n'
            f'  (in_bom yes) (on_board yes) (dnp no)\n'
            f'  (uuid "{symbol_uuid}")\n'
            f'  (property "Reference" "{pwr_ref}" (at {x} {y - 2} 0)\n'
            f'    (effects (font (size 1.27 1.27)) hide)\n'
            f'  )\n'
            f'  (property "Value" "{name}" (at {x} {y + 2} 0)\n'
            f'    (effects (font (size 1.27 1.27)))\n'
            f'  )\n'
            f'  (instances\n'
            f'    (project ""\n'
            f'      (path "/{sch_uuid}"\n'
            f'        (reference "{pwr_ref}") (unit 1)\n'
            f'      )\n'
            f'    )\n'
            f'  )\n'
            f')'
        )
        self._parse_and_create(sym_sexp)
        return {
            "name": name,
            "lib_id": lib_id,
            "reference": pwr_ref,
            "position": {"x": x, "y": y},
            "rotation": rotation,
            "uuid": symbol_uuid,
        }

    def add_junction(self, path: Path, x: float, y: float) -> dict[str, Any]:
        try:
            jn_uuid = str(_uuid.uuid4())
            sexp = (
                f'(junction (at {x} {y}) (diameter 0) (color 0 0 0 0)\n'
                f'  (uuid "{jn_uuid}")\n'
                f')'
            )
            self._parse_and_create(sexp)
            return {
                "position": {"x": x, "y": y},
                "uuid": jn_uuid,
            }
        except Exception as e:
            logger.warning("IPC add_junction failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.add_junction(path, x, y)

    # -- Delete operations (SaveDocumentToString + find UUID + DeleteItems)

    def remove_component(self, path: Path, reference: str) -> dict[str, Any]:
        try:
            from kicad_mcp.utils.sexp_parser import find_symbol_block_by_reference
            content = self._get_doc_as_string()
            location = find_symbol_block_by_reference(content, reference)
            if location is None:
                raise ValueError(f"Symbol with reference '{reference}' not found in schematic")
            start, end = location
            uuid_str = self._find_uuid_in_sexp(content, start, end)
            if uuid_str is None:
                raise ValueError(f"Symbol '{reference}' has no UUID")
            self._delete_by_uuid(uuid_str)
            return {"reference": reference, "removed": True}
        except Exception as e:
            logger.warning("IPC remove_component failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.remove_component(path, reference)

    def remove_wire(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> dict[str, Any]:
        try:
            from kicad_mcp.utils.sexp_parser import find_wire_block_by_endpoints
            content = self._get_doc_as_string()
            location = find_wire_block_by_endpoints(content, start_x, start_y, end_x, end_y)
            if location is None:
                raise ValueError(
                    f"Wire from ({start_x}, {start_y}) to ({end_x}, {end_y}) not found"
                )
            start, end = location
            uuid_str = self._find_uuid_in_sexp(content, start, end)
            if uuid_str is None:
                raise ValueError("Wire has no UUID")
            self._delete_by_uuid(uuid_str)
            return {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
                "removed": True,
            }
        except Exception as e:
            logger.warning("IPC remove_wire failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.remove_wire(path, start_x, start_y, end_x, end_y)

    def remove_no_connect(self, path: Path, x: float, y: float) -> dict[str, Any]:
        try:
            from kicad_mcp.utils.sexp_parser import find_no_connect_block_by_position
            content = self._get_doc_as_string()
            location = find_no_connect_block_by_position(content, x, y)
            if location is None:
                raise ValueError(f"No-connect at ({x}, {y}) not found")
            start, end = location
            uuid_str = self._find_uuid_in_sexp(content, start, end)
            if uuid_str is None:
                raise ValueError("No-connect has no UUID")
            self._delete_by_uuid(uuid_str)
            return {"position": {"x": x, "y": y}, "removed": True}
        except Exception as e:
            logger.warning("IPC remove_no_connect failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.remove_no_connect(path, x, y)

    # -- Modify operations (delete + re-create via S-expression) ----------

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        try:
            return self._move_component_via_ipc(path, reference, x, y, rotation)
        except Exception as e:
            logger.warning("IPC move_component (schematic) failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.move_component(path, reference, x, y, rotation)

    def _move_component_via_ipc(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        from kicad_mcp.utils.sexp_parser import find_symbol_block_by_reference

        content = self._get_doc_as_string()
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in schematic")
        start_idx, end_idx = location
        block = content[start_idx:end_idx + 1]

        # Extract the UUID to delete the old item
        uuid_str = self._find_uuid_in_sexp(content, start_idx, end_idx)
        if uuid_str is None:
            raise ValueError(f"Symbol '{reference}' has no UUID")

        # Parse old position from the block
        at_match = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)', block)
        if at_match is None:
            raise ValueError(f"Symbol '{reference}' has no (at ...) clause")

        old_x = float(at_match.group(1))
        old_y = float(at_match.group(2))
        old_rot = float(at_match.group(3)) if at_match.group(3) else 0.0
        new_rot = rotation if rotation is not None else old_rot
        dx = x - old_x
        dy = y - old_y

        # Build the updated block: replace the symbol-level (at ...)
        new_at = f"(at {x} {y} {new_rot})"
        new_block = block[:at_match.start()] + new_at + block[at_match.end():]

        # Shift all property (at ...) positions by the same delta
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

        # Assign a new UUID (KiCad may reject re-creating with same UUID)
        new_uuid = str(_uuid.uuid4())
        new_block = re.sub(
            r'\(uuid\s+"[^"]+"\)', f'(uuid "{new_uuid}")', new_block, count=1
        )

        # Delete old, create new
        self._delete_by_uuid(uuid_str)
        self._parse_and_create(new_block)

        return {
            "reference": reference,
            "position": {"x": x, "y": y},
            "rotation": new_rot,
        }

    def update_component_property(
        self, path: Path, reference: str,
        property_name: str, property_value: str,
    ) -> dict[str, Any]:
        try:
            return self._update_component_property_via_ipc(path, reference, property_name, property_value)
        except Exception as e:
            logger.warning("IPC update_component_property failed (%s), falling back to file ops", e)
            if _is_token_error(e):
                _reset_kicad()
            return self._file_ops.update_component_property(path, reference, property_name, property_value)

    def _update_component_property_via_ipc(
        self, path: Path, reference: str,
        property_name: str, property_value: str,
    ) -> dict[str, Any]:
        from kicad_mcp.utils.sexp_parser import find_symbol_block_by_reference

        content = self._get_doc_as_string()
        location = find_symbol_block_by_reference(content, reference)
        if location is None:
            raise ValueError(f"Symbol with reference '{reference}' not found in schematic")
        start_idx, end_idx = location
        block = content[start_idx:end_idx + 1]

        # Extract UUID for deletion
        uuid_str = self._find_uuid_in_sexp(content, start_idx, end_idx)
        if uuid_str is None:
            raise ValueError(f"Symbol '{reference}' has no UUID")

        # Update or add the property in the block
        escaped_name = re.escape(property_name)
        prop_pattern = re.compile(
            rf'(\(property\s+"{escaped_name}"\s+)"([^"]*)"'
        )
        match = prop_pattern.search(block)

        if match:
            new_block = (
                block[:match.start(2)]
                + property_value
                + block[match.end(2):]
            )
        else:
            # Add new property before the closing paren
            at_match = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)', block)
            px = float(at_match.group(1)) if at_match else 0
            py = float(at_match.group(2)) + 6 if at_match else 0
            new_prop = (
                f'  (property "{property_name}" "{property_value}" (at {px} {py} 0)\n'
                f'    (effects (font (size 1.27 1.27)) hide)\n'
                f'  )\n'
            )
            new_block = block[:-1].rstrip() + "\n" + new_prop + ")"

        # Assign a new UUID
        new_uuid = str(_uuid.uuid4())
        new_block = re.sub(
            r'\(uuid\s+"[^"]+"\)', f'(uuid "{new_uuid}")', new_block, count=1
        )

        # Delete old, create new
        self._delete_by_uuid(uuid_str)
        self._parse_and_create(new_block)

        return {
            "reference": reference,
            "property": property_name,
            "value": property_value,
        }

    # -- Delegate creation/annotation to file backend (with save/revert) --

    def create_schematic(
        self, path: Path, title: str = "", revision: str = "",
    ) -> dict[str, Any]:
        # Creating a new file doesn't need IPC - no conflict possible
        return self._file_ops.create_schematic(path, title, revision)

    def annotate(self, path: Path) -> dict[str, Any]:
        self._try_save_to_disk()
        result = self._file_ops.annotate(path)
        # Reload in KiCad after file-based annotation
        from kipy.proto.common.commands.editor_commands_pb2 import RevertDocument
        from google.protobuf.empty_pb2 import Empty
        try:
            doc = self._get_schematic_doc()
            command = RevertDocument()
            command.document.CopyFrom(doc)
            self._get_client().send(command, Empty)
        except Exception:
            logger.debug("Could not revert schematic after annotate (may not be open)")
        return result

    def generate_netlist(self, path: Path, output: Path) -> dict[str, Any]:
        self._try_save_to_disk()
        return self._file_ops.generate_netlist(path, output)


class IPCBackend(KiCadBackend):
    """Backend using KiCad 9+ IPC API via kipy."""

    @property
    def name(self) -> str:
        return "ipc"

    @property
    def capabilities(self) -> set[BackendCapability]:
        return {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.SCHEMATIC_READ,
            BackendCapability.SCHEMATIC_MODIFY,
            BackendCapability.LIBRARY_SEARCH,
            BackendCapability.REAL_TIME_SYNC,
            BackendCapability.ZONE_REFILL,
            BackendCapability.BOARD_STACKUP,
        }

    def is_available(self) -> bool:
        try:
            import kipy  # noqa: F401
            # Test actual connection
            kicad = _get_kicad()
            return True
        except Exception:
            return False

    def get_version(self) -> str | None:
        try:
            kicad = _get_kicad()
            if hasattr(kicad, "get_version"):
                v = kicad.get_version()
                # kipy returns a KiCadVersion protobuf; convert to a plain string
                if hasattr(v, "full_version"):
                    return str(v.full_version)
                return str(v)
        except Exception:
            pass
        return None

    @staticmethod
    def _doc_path(doc: Any) -> str:
        """Extract a path-like field from a KiCad document object."""
        for attr in ("path", "board_path", "file_path"):
            value = getattr(doc, attr, None)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _add_open_document(open_documents: list[dict[str, str]], doc_type: str, path: str) -> None:
        entry = {"type": doc_type, "path": path}
        if entry not in open_documents:
            open_documents.append(entry)

    @staticmethod
    def _is_open_documents_unsupported(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "no handler available" in msg or "getopendocuments" in msg

    def _fallback_project_from_board(self, kicad: Any) -> tuple[str | None, str | None, str | None]:
        """Best-effort project discovery from the active board document.

        Linux KiCad IPC builds may not expose GetOpenDocuments, but the active
        board's document metadata still carries project information.
        """
        try:
            board = kicad.get_board()
        except Exception as e:
            logger.debug("Board fallback unavailable for active project query: %s", e)
            return None, None, None

        doc = getattr(board, "document", None)
        if doc is None:
            return None, None, None

        project = getattr(doc, "project", None)
        project_name = getattr(project, "name", None) if project is not None else None
        project_path_obj = getattr(project, "path", None) if project is not None else None
        project_path = str(project_path_obj) if project_path_obj else None
        board_path = self._doc_path(doc) or None
        return project_name, project_path, board_path

    def get_active_project(self) -> dict[str, Any]:
        """Query the currently open KiCad project via IPC API.

        Returns:
            Dict with project_name, project_path, and open_documents list.
        """
        kicad = _get_kicad()

        project_name = None
        project_path = None
        open_documents: list[dict[str, str]] = []
        open_documents_supported = True

        # Primary source: board document metadata (works even when
        # GetOpenDocuments is not implemented on Linux IPC builds).
        fb_name, fb_path, fb_board_path = self._fallback_project_from_board(kicad)
        project_name = fb_name
        project_path = fb_path
        if fb_board_path:
            self._add_open_document(open_documents, "pcb", fb_board_path)

        # Query open project documents
        try:
            from kipy.proto.common.types import DocumentType
            project_docs = kicad.get_open_documents(DocumentType.DOCTYPE_PROJECT)
            if project_docs:
                doc = project_docs[0]
                project_info = kicad.get_project(doc)
                project_name = getattr(project_info, "name", None)
                project_path = getattr(project_info, "path", None)
                if project_path:
                    project_path = str(project_path)
                self._add_open_document(open_documents, "project", project_path or self._doc_path(doc))
        except Exception as e:
            logger.debug("Could not query project documents: %s", e)
            if self._is_open_documents_unsupported(e):
                open_documents_supported = False

        # Query open schematic documents
        if open_documents_supported:
            try:
                from kipy.proto.common.types import DocumentType
                sch_docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
                for doc in sch_docs:
                    self._add_open_document(open_documents, "schematic", self._doc_path(doc))
            except Exception as e:
                logger.debug("Could not query schematic documents: %s", e)

        # Query open PCB documents
        if open_documents_supported:
            try:
                from kipy.proto.common.types import DocumentType
                pcb_docs = kicad.get_open_documents(DocumentType.DOCTYPE_PCB)
                for doc in pcb_docs:
                    self._add_open_document(open_documents, "pcb", self._doc_path(doc))
            except Exception as e:
                logger.debug("Could not query PCB documents: %s", e)

        return {
            "project_name": project_name,
            "project_path": project_path,
            "open_documents": open_documents,
        }

    def get_text_variables(self, project_path: Path) -> dict[str, Any]:
        """Get project text variables (${VAR} substitution table)."""
        try:
            kicad = _get_kicad()
            project = None
            try:
                from kipy.proto.common.types import DocumentType
                docs = kicad.get_open_documents(DocumentType.DOCTYPE_PROJECT)
                if docs:
                    project = kicad.get_project(docs[0])
            except Exception as e:
                logger.debug("Could not query project document for text vars: %s", e)

            if project is None:
                board = kicad.get_board()
                doc = getattr(board, "document", None)
                project = getattr(doc, "project", None)

            if project is None:
                return {"status": "unavailable", "reason": "No project is open in KiCad"}
            if hasattr(project, "get_text_variables"):
                variables = dict(project.get_text_variables())
                return {"status": "success", "variables": variables}
            return {"status": "unavailable", "reason": "Project does not support text variables"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def set_text_variables(self, project_path: Path, variables: dict[str, str]) -> dict[str, Any]:
        """Set project text variables."""
        try:
            kicad = _get_kicad()
            project = None
            try:
                from kipy.proto.common.types import DocumentType
                docs = kicad.get_open_documents(DocumentType.DOCTYPE_PROJECT)
                if docs:
                    project = kicad.get_project(docs[0])
            except Exception as e:
                logger.debug("Could not query project document for text vars: %s", e)

            if project is None:
                board = kicad.get_board()
                doc = getattr(board, "document", None)
                project = getattr(doc, "project", None)

            if project is None:
                return {"status": "unavailable", "reason": "No project is open in KiCad"}
            if hasattr(project, "set_text_variables"):
                project.set_text_variables(variables)
                return {"status": "success", "variables_set": len(variables)}
            return {"status": "unavailable", "reason": "Project does not support text variables"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_board_ops(self) -> IPCBoardOps:
        return IPCBoardOps()

    def get_schematic_ops(self) -> IPCSchematicOps:
        from kicad_mcp.backends.file_backend import FileSchematicOps
        return IPCSchematicOps(FileSchematicOps())

    def get_library_ops(self) -> IPCLibraryOps:
        return IPCLibraryOps()
