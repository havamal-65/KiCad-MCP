"""SWIG backend for KiCad 7-8 via pcbnew Python bindings."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import (
    BackendCapability,
    BoardOps,
    ExportOps,
    KiCadBackend,
    LibraryOps,
)
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.platform_helper import add_kicad_to_sys_path

logger = get_logger("backend.swig")

# Lazy import - pcbnew may not be available
_pcbnew = None


def _get_pcbnew():
    global _pcbnew
    if _pcbnew is None:
        add_kicad_to_sys_path()
        import pcbnew
        _pcbnew = pcbnew
    return _pcbnew


class SWIGBoardOps(BoardOps):
    """Board operations via pcbnew SWIG bindings."""

    def read_board(self, path: Path) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        info = self._extract_board_info(board, path)
        components = self._extract_components(board)
        nets = self._extract_nets(board)
        tracks = self._extract_tracks(board)
        return {
            "info": info,
            "components": components,
            "nets": nets,
            "tracks": tracks,
        }

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        return self._extract_components(board)

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        return self._extract_nets(board)

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        return self._extract_tracks(board)

    def get_board_info(self, path: Path) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        return self._extract_board_info(board, path)

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))

        fp = pcbnew.FootprintLoad(
            str(Path(footprint).parent) if ":" not in footprint else "",
            footprint.split(":")[-1] if ":" in footprint else footprint,
        )
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        fp.SetReference(reference)
        if rotation:
            fp.SetOrientationDegrees(rotation)

        board.Add(fp)
        board.Save(str(path))

        return {
            "reference": reference,
            "footprint": footprint,
            "position": {"x": x, "y": y},
        }

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))

        for fp in board.GetFootprints():
            if fp.GetReference() == reference:
                fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                if rotation is not None:
                    fp.SetOrientationDegrees(rotation)
                board.Save(str(path))
                return {
                    "reference": reference,
                    "position": {"x": x, "y": y},
                    "rotation": rotation,
                }

        raise ValueError(f"Component {reference} not found on board")

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))

        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start_x), pcbnew.FromMM(start_y)))
        track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end_x), pcbnew.FromMM(end_y)))
        track.SetWidth(pcbnew.FromMM(width))
        track.SetLayer(board.GetLayerID(layer))

        if net:
            netinfo = board.FindNet(net)
            if netinfo:
                track.SetNet(netinfo)

        board.Add(track)
        board.Save(str(path))

        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "width": width,
            "layer": layer,
        }

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))

        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        via.SetWidth(pcbnew.FromMM(size))
        via.SetDrill(pcbnew.FromMM(drill))

        if net:
            netinfo = board.FindNet(net)
            if netinfo:
                via.SetNet(netinfo)

        board.Add(via)
        board.Save(str(path))

        return {
            "position": {"x": x, "y": y},
            "size": size,
            "drill": drill,
        }

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        ds = board.GetDesignSettings()

        return {
            "min_track_width": pcbnew.ToMM(ds.m_TrackMinWidth),
            "min_via_diameter": pcbnew.ToMM(ds.m_ViasMinSize),
            "min_via_drill": pcbnew.ToMM(ds.m_MinThroughDrill),
            "min_clearance": pcbnew.ToMM(ds.GetDefault().GetClearance()),
        }

    def _extract_board_info(self, board: Any, path: Path) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        title_block = board.GetTitleBlock()
        return {
            "file_path": str(path),
            "title": title_block.GetTitle(),
            "revision": title_block.GetRevision(),
            "date": title_block.GetDate(),
            "num_components": len(board.GetFootprints()),
            "num_nets": board.GetNetCount(),
            "num_tracks": len(board.GetTracks()),
            "layers": [board.GetLayerName(i) for i in board.GetEnabledLayers().Seq()],
        }

    def _extract_components(self, board: Any) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        components = []
        for fp in board.GetFootprints():
            pos = fp.GetPosition()
            components.append({
                "reference": fp.GetReference(),
                "value": fp.GetValue(),
                "footprint": str(fp.GetFPID().GetUniStringLibId()),
                "position": {"x": pcbnew.ToMM(pos.x), "y": pcbnew.ToMM(pos.y)},
                "layer": fp.GetLayerName(),
                "rotation": fp.GetOrientationDegrees(),
            })
        return components

    def _extract_nets(self, board: Any) -> list[dict[str, Any]]:
        nets = []
        netinfo = board.GetNetInfo()
        for net in netinfo.NetsByNetcode():
            if net > 0:  # Skip unconnected net 0
                info = netinfo.GetNetItem(net)
                nets.append({
                    "number": net,
                    "name": info.GetNetname(),
                })
        return nets

    def _extract_tracks(self, board: Any) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        tracks = []
        for track in board.GetTracks():
            start = track.GetStart()
            end = track.GetEnd()
            tracks.append({
                "start": {"x": pcbnew.ToMM(start.x), "y": pcbnew.ToMM(start.y)},
                "end": {"x": pcbnew.ToMM(end.x), "y": pcbnew.ToMM(end.y)},
                "width": pcbnew.ToMM(track.GetWidth()),
                "layer": track.GetLayerName(),
                "net": track.GetNetname(),
            })
        return tracks


class SWIGExportOps(ExportOps):
    """Export operations via pcbnew SWIG bindings."""

    def export_gerbers(
        self, board_path: Path, output_dir: Path, layers: list[str] | None = None,
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(board_path))
        output_dir.mkdir(parents=True, exist_ok=True)

        plot_controller = pcbnew.PLOT_CONTROLLER(board)
        plot_options = plot_controller.GetPlotOptions()
        plot_options.SetOutputDirectory(str(output_dir))
        plot_options.SetPlotFrameRef(False)
        plot_options.SetUseGerberProtelExtensions(True)

        enabled_layers = layers or [
            board.GetLayerName(i) for i in board.GetEnabledLayers().Seq()
        ]
        output_files = []

        for layer_name in enabled_layers:
            layer_id = board.GetLayerID(layer_name)
            if layer_id < 0:
                continue
            plot_controller.OpenPlotfile(layer_name, pcbnew.PLOT_FORMAT_GERBER, layer_name)
            plot_controller.PlotLayer()
            plot_controller.ClosePlot()
            # Find generated file
            for f in output_dir.iterdir():
                if f.is_file() and str(f) not in output_files:
                    output_files.append(str(f))

        return {
            "success": True,
            "output_dir": str(output_dir),
            "output_files": output_files,
        }

    def export_bom(
        self, path: Path, output: Path, fmt: str = "csv",
    ) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        board = pcbnew.LoadBoard(str(path))
        output.parent.mkdir(parents=True, exist_ok=True)

        components: dict[str, dict[str, Any]] = {}
        for fp in board.GetFootprints():
            key = f"{fp.GetValue()}|{fp.GetFPID().GetUniStringLibId()}"
            if key in components:
                components[key]["quantity"] += 1
                components[key]["references"].append(fp.GetReference())
            else:
                components[key] = {
                    "value": fp.GetValue(),
                    "footprint": str(fp.GetFPID().GetUniStringLibId()),
                    "quantity": 1,
                    "references": [fp.GetReference()],
                }

        if fmt == "csv":
            import csv
            with open(output, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["References", "Value", "Footprint", "Quantity"])
                for comp in components.values():
                    writer.writerow([
                        ", ".join(sorted(comp["references"])),
                        comp["value"],
                        comp["footprint"],
                        comp["quantity"],
                    ])
        else:
            import json as json_mod
            with open(output, "w", encoding="utf-8") as f:
                json_mod.dump(list(components.values()), f, indent=2)

        return {
            "success": True,
            "output_files": [str(output)],
        }


class SWIGLibraryOps(LibraryOps):
    """Library operations via pcbnew SWIG bindings."""

    def search_symbols(self, query: str) -> list[dict[str, Any]]:
        # SWIG doesn't have great symbol search; delegate to file backend
        raise NotImplementedError("Use file backend for symbol search")

    def search_footprints(self, query: str) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        results = []
        query_lower = query.lower()

        try:
            fp_libs = pcbnew.GetFootprintLibraries()
            for lib in fp_libs:
                fps = pcbnew.GetFootprints(lib)
                for fp_name in fps:
                    if query_lower in fp_name.lower():
                        results.append({
                            "name": fp_name,
                            "library": lib,
                            "lib_id": f"{lib}:{fp_name}",
                        })
        except Exception as e:
            logger.debug("Footprint search error: %s", e)

        return results[:50]

    def list_libraries(self) -> list[dict[str, Any]]:
        pcbnew = _get_pcbnew()
        libs = []
        try:
            for lib in pcbnew.GetFootprintLibraries():
                libs.append({"name": lib, "type": "footprint"})
        except Exception as e:
            logger.debug("Library listing error: %s", e)
        return libs

    def get_footprint_info(self, lib_id: str) -> dict[str, Any]:
        pcbnew = _get_pcbnew()
        parts = lib_id.split(":", 1)
        if len(parts) != 2:
            return {"error": f"Invalid lib_id: {lib_id}"}
        lib_name, fp_name = parts
        try:
            fp = pcbnew.FootprintLoad(lib_name, fp_name)
            pads = list(fp.Pads())
            return {
                "name": fp_name,
                "library": lib_name,
                "description": fp.GetDescription(),
                "keywords": fp.GetKeywords(),
                "pad_count": len(pads),
                "smd": any(p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD for p in pads),
            }
        except Exception as e:
            return {"error": str(e)}


class SWIGBackend(KiCadBackend):
    """Backend using KiCad's SWIG/pcbnew Python bindings (KiCad 7-8)."""

    @property
    def name(self) -> str:
        return "swig"

    @property
    def capabilities(self) -> set[BackendCapability]:
        return {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.EXPORT_GERBER,
            BackendCapability.EXPORT_BOM,
            BackendCapability.LIBRARY_SEARCH,
        }

    def is_available(self) -> bool:
        try:
            add_kicad_to_sys_path()
            import pcbnew  # noqa: F401
            return True
        except ImportError:
            return False

    def get_version(self) -> str | None:
        try:
            pcbnew = _get_pcbnew()
            return pcbnew.Version()
        except Exception:
            return None

    def get_board_ops(self) -> SWIGBoardOps:
        return SWIGBoardOps()

    def get_export_ops(self) -> SWIGExportOps:
        return SWIGExportOps()

    def get_library_ops(self) -> SWIGLibraryOps:
        return SWIGLibraryOps()
