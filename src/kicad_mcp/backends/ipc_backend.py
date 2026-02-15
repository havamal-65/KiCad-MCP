"""IPC backend for KiCad 9+ via kipy (kicad-python)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    KiCadBackend,
    LibraryOps,
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


class IPCBoardOps(BoardOps):
    """Board operations via KiCad 9+ IPC API."""

    def read_board(self, path: Path) -> dict[str, Any]:
        kicad = _get_kicad()
        board = kicad.get_board()
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
        kicad = _get_kicad()
        board = kicad.get_board()
        return {
            "file_path": str(path),
            "title": board.get_title() if hasattr(board, "get_title") else "",
            "num_components": len(board.get_footprints()) if hasattr(board, "get_footprints") else 0,
            "num_nets": len(board.get_nets()) if hasattr(board, "get_nets") else 0,
            "num_tracks": len(board.get_tracks()) if hasattr(board, "get_tracks") else 0,
        }

    def get_components(self, path: Path) -> list[dict[str, Any]]:
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

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
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

        raise NotImplementedError("IPC board does not support place_footprint")

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
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

        raise ValueError(f"Component {reference} not found")

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
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

        raise NotImplementedError("IPC board does not support add_track")

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
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

        raise NotImplementedError("IPC board does not support add_via")

    def get_design_rules(self, path: Path) -> dict[str, Any]:
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


class IPCLibraryOps(LibraryOps):
    """Library operations via KiCad 9+ IPC API."""

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
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

    def search_footprints(self, query: str) -> list[dict[str, Any]]:
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

    def list_libraries(self) -> list[dict[str, Any]]:
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
            BackendCapability.LIBRARY_SEARCH,
            BackendCapability.REAL_TIME_SYNC,
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
                return kicad.get_version()
        except Exception:
            pass
        return None

    def get_board_ops(self) -> IPCBoardOps:
        return IPCBoardOps()

    def get_library_ops(self) -> IPCLibraryOps:
        return IPCLibraryOps()
