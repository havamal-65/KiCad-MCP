"""KiCad MCP Bridge Plugin.

This plugin runs inside KiCad's embedded Python interpreter and exposes a
local TCP server on localhost:9760 (configurable via KICAD_MCP_PLUGIN_PORT).

The MCP PluginBackend connects to this server to perform board operations
directly via the pcbnew API — no gRPC, no file-parsing, no subprocess.

Supported methods
-----------------
Read:
  ping               → {pong, kicad_version}
  get_board_info     → board metadata
  get_components     → list of placed footprints
  get_nets           → list of nets
  get_tracks         → list of track segments and vias
  get_design_rules   → default netclass rules
  get_stackup        → layer stackup
  get_active_project → open project/board info

Write (all run on the wxPython main thread to avoid pcbnew GUI crashes):
  place_component    → load footprint from library, add to board
  move_component     → reposition / re-orient a footprint
  add_track          → add a copper track segment
  add_via            → add a through/blind/buried via
  assign_net         → assign a net to a specific pad
  refill_zones       → fill all copper zones

Installation (Windows):
    Copy to %APPDATA%\\kicad\\9.0\\scripting\\plugins\\
    Restart KiCad — server starts automatically at plugin load time.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
from typing import Any

logger = logging.getLogger(__name__)

_PORT = int(os.environ.get("KICAD_MCP_PLUGIN_PORT", "9760"))
_server_thread: threading.Thread | None = None
_tcp_server: socketserver.TCPServer | None = None


# ---------------------------------------------------------------------------
# Thread safety — write ops must run on the wxPython main thread
# ---------------------------------------------------------------------------

def _run_on_main_thread(fn):
    """Run *fn* on the wx main thread and return its result (or re-raise).

    Write operations that touch pcbnew's in-memory board state must run on the
    GUI thread or they crash the process.  Falls back to direct execution when
    wx is not available (e.g. unit tests with a mock server).
    """
    try:
        import wx
    except ImportError:
        return fn()

    result: list = [None]
    error: list = [None]
    done = threading.Event()

    def _wrapper():
        try:
            result[0] = fn()
        except Exception as exc:
            error[0] = exc
        finally:
            done.set()

    wx.CallAfter(_wrapper)
    done.wait(timeout=30)

    if error[0] is not None:
        raise error[0]
    return result[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_open_board(path: str):
    """Return the currently open pcbnew Board, verifying it matches *path*."""
    import pcbnew
    board = pcbnew.GetBoard()
    if board is None:
        raise ValueError("No board is currently open in KiCad")
    board_path = board.GetFileName()
    if os.path.normcase(os.path.normpath(board_path)) != os.path.normcase(os.path.normpath(path)):
        raise ValueError(
            f"Requested board '{path}' does not match open board '{board_path}'. "
            "Open the correct .kicad_pcb file in KiCad first."
        )
    return board


def _save_and_refresh(board) -> None:
    """Save board to disk, then refresh KiCad's display on the main thread."""
    import pcbnew
    board.Save(board.GetFileName())
    # Schedule Refresh on the main thread — calling it from a background thread
    # crashes pcbnew on Windows.
    try:
        import wx
        wx.CallAfter(pcbnew.Refresh)
    except Exception:
        pass


def _mm(value: float) -> int:
    """Convert mm to pcbnew internal units (nm), always returning int.

    pcbnew.VECTOR2I and related constructors are C++ templates over int;
    passing a float from pcbnew.FromMM() without casting causes a SWIG
    type error that hard-crashes the interpreter.
    """
    import pcbnew
    return int(pcbnew.FromMM(value))


def _layer_id(board, layer_name: str) -> int:
    """Resolve a layer name string to a pcbnew layer ID integer."""
    import pcbnew
    _KNOWN = {
        "F.Cu": pcbnew.F_Cu,
        "B.Cu": pcbnew.B_Cu,
        "F.SilkS": pcbnew.F_SilkS,
        "B.SilkS": pcbnew.B_SilkS,
        "Edge.Cuts": pcbnew.Edge_Cuts,
    }
    if layer_name in _KNOWN:
        return _KNOWN[layer_name]
    lid = board.GetLayerID(layer_name)
    if lid < 0:
        raise ValueError(f"Unknown layer: {layer_name!r}")
    return lid


def _load_footprint(lib_id: str):
    """Load a footprint by 'Library:Name', trying multiple KiCad API paths."""
    import pcbnew
    if ":" not in lib_id:
        raise ValueError(f"footprint must be 'Library:Name' format, got: {lib_id!r}")
    lib_nick, fp_name = lib_id.split(":", 1)

    # Strategy 1: global footprint table (KiCad 8 API name)
    lib_path = None
    try:
        table = pcbnew.GetGlobalFootprintTable()
        lib_path = table.FindRow(lib_nick).GetFullURI(True)
    except AttributeError:
        pass

    # Strategy 2: FOOTPRINT_LIB_TABLE static accessor (KiCad 9 API)
    if lib_path is None:
        for attr in ("GetGlobalTable", "GetUserTable"):
            try:
                table = getattr(pcbnew.FOOTPRINT_LIB_TABLE, attr)()
                lib_path = table.FindRow(lib_nick).GetFullURI(True)
                break
            except Exception:
                pass

    # Strategy 3: search standard KiCad footprint directories by nickname
    if lib_path is None:
        _search_dirs = [
            os.environ.get("KICAD9_FOOTPRINT_DIR", ""),
            os.environ.get("KICAD8_FOOTPRINT_DIR", ""),
            r"C:\Program Files\KiCad\9.0\share\kicad\footprints",
            r"C:\Program Files\KiCad\8.0\share\kicad\footprints",
            "/usr/share/kicad/footprints",
            "/usr/local/share/kicad/footprints",
        ]
        for base in _search_dirs:
            if not base:
                continue
            candidate = os.path.join(base, lib_nick + ".pretty")
            if os.path.isdir(candidate):
                lib_path = candidate
                break

    if lib_path is None:
        raise ValueError(
            f"Could not find library {lib_nick!r}. "
            "Set KICAD9_FOOTPRINT_DIR to your KiCad footprints directory."
        )

    fp = pcbnew.FootprintLoad(lib_path, fp_name)
    if fp is None:
        raise ValueError(f"Footprint {fp_name!r} not found in {lib_path!r}")
    return fp


# ---------------------------------------------------------------------------
# Read handlers (safe to call from any thread)
# ---------------------------------------------------------------------------

def _handle_ping() -> dict[str, Any]:
    try:
        import pcbnew
        version = pcbnew.GetBuildVersion()
    except Exception:
        version = "unknown"
    return {"pong": True, "kicad_version": version}


def _handle_get_board_info(path: str) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        title_block = board.GetTitleBlock()
        bb = board.GetBoardEdgesBoundingBox()
        return {
            "title": title_block.GetTitle(),
            "revision": title_block.GetRevision(),
            "layer_count": board.GetCopperLayerCount(),
            "width_mm": round(pcbnew.ToMM(bb.GetWidth()), 4),
            "height_mm": round(pcbnew.ToMM(bb.GetHeight()), 4),
            "net_count": board.GetNetCount(),
            "footprint_count": len(list(board.GetFootprints())),
        }
    return _run_on_main_thread(_do)


def _handle_get_components(path: str) -> list[dict[str, Any]]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        components = []
        for fp in board.GetFootprints():
            pos = fp.GetPosition()
            components.append({
                "reference": fp.GetReference(),
                "value": fp.GetValue(),
                "x": round(pcbnew.ToMM(pos.x), 4),
                "y": round(pcbnew.ToMM(pos.y), 4),
                "layer": fp.GetLayerName(),
                "rotation": round(fp.GetOrientationDegrees(), 4),
            })
        return components
    return _run_on_main_thread(_do)


def _handle_get_nets(path: str) -> list[dict[str, Any]]:
    def _do():
        board = _get_open_board(path)
        nets = []
        for net_id, net in board.GetNetInfo().NetsByNetcode().items():
            nets.append({"net_id": net_id, "name": net.GetNetname()})
        return nets
    return _run_on_main_thread(_do)


def _handle_get_tracks(path: str) -> list[dict[str, Any]]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        tracks = []
        for item in board.GetTracks():
            if isinstance(item, pcbnew.PCB_VIA):
                pos = item.GetPosition()
                tracks.append({
                    "type": "via",
                    "x": round(pcbnew.ToMM(pos.x), 4),
                    "y": round(pcbnew.ToMM(pos.y), 4),
                    "size": round(pcbnew.ToMM(item.GetWidth()), 4),
                    "drill": round(pcbnew.ToMM(item.GetDrillValue()), 4),
                    "net": item.GetNetname(),
                })
            else:
                start = item.GetStart()
                end = item.GetEnd()
                tracks.append({
                    "type": "track",
                    "start_x": round(pcbnew.ToMM(start.x), 4),
                    "start_y": round(pcbnew.ToMM(start.y), 4),
                    "end_x": round(pcbnew.ToMM(end.x), 4),
                    "end_y": round(pcbnew.ToMM(end.y), 4),
                    "width": round(pcbnew.ToMM(item.GetWidth()), 4),
                    "layer": item.GetLayerName(),
                    "net": item.GetNetname(),
                })
        return tracks
    return _run_on_main_thread(_do)


def _handle_get_design_rules(path: str) -> dict[str, Any]:
    """Return design rules, trying multiple netclass API paths for KiCad compatibility."""
    import pcbnew
    board = _get_open_board(path)
    ds = board.GetDesignSettings()
    result: dict[str, Any] = {}

    # Direct min-value attributes — present in most KiCad versions
    for attr, key in [
        ("m_TrackMinWidth",   "min_track_width_mm"),
        ("m_ViasMinSize",     "min_via_diameter_mm"),
        ("m_MinThroughDrill", "min_via_drill_mm"),
        ("m_MinClearance",    "clearance_mm"),          # KiCad 9: clearance on ds directly
    ]:
        try:
            val = pcbnew.ToMM(getattr(ds, attr))
            result[key] = round(val, 4)
        except Exception:
            pass

    # Default netclass — try multiple API paths for KiCad 8/9 compatibility
    # Each path may raise AttributeError, TypeError, or return None/a bad object
    nc = None
    def _try_nc_paths():
        # KiCad 8: netclass on design settings
        try:
            obj = ds.GetNetClasses().GetDefault()
            if obj is not None:
                return obj
        except Exception:
            pass
        # KiCad 8 alt: netclass on board
        try:
            obj = board.GetNetClasses().GetDefault()
            if obj is not None:
                return obj
        except Exception:
            pass
        # KiCad 9: m_NetSettings.m_DefaultNetClass (may be a dict or object)
        try:
            obj = ds.m_NetSettings.m_DefaultNetClass
            if obj is not None:
                return obj
        except Exception:
            pass
        # KiCad 9 alt: GetDefaultNetClass helper
        try:
            obj = ds.GetDefaultNetClass()
            if obj is not None:
                return obj
        except Exception:
            pass
        # KiCad 9: net info default netclass
        try:
            obj = board.GetNetInfo().GetNetClass("")
            if obj is not None:
                return obj
        except Exception:
            pass
        return None
    nc = _try_nc_paths()

    if nc is not None:
        for method, key in [
            ("GetClearance",    "clearance_mm"),
            ("GetTrackWidth",   "track_width_mm"),
            ("GetViaDiameter",  "via_diameter_mm"),
            ("GetViaDrill",     "via_drill_mm"),
        ]:
            if key in result:
                continue  # already populated from direct attr; don't overwrite
            try:
                result[key] = round(pcbnew.ToMM(getattr(nc, method)()), 4)
            except Exception:
                pass

    if "clearance_mm" not in result:
        result["netclass_note"] = (
            "Default netclass clearance not accessible via known API paths. "
            "Min-value fields above are still valid."
        )

    return result


def _handle_get_stackup(path: str) -> dict[str, Any]:
    """Return stackup layers, with fallback to copper-layer enumeration."""
    import pcbnew
    board = _get_open_board(path)
    layers: list[dict[str, Any]] = []

    # Primary: try stackup descriptor
    try:
        stackup = board.GetDesignSettings().GetStackupDescriptor()
        count = stackup.GetCount()
        for i in range(count):
            sl = stackup.GetStackupLayer(i)
            entry: dict[str, Any] = {}
            for method, key in [("GetName", "name"), ("GetTypeName", "type"), ("GetMaterial", "material")]:
                try:
                    entry[key] = getattr(sl, method)()
                except Exception:
                    pass
            try:
                entry["thickness_mm"] = round(pcbnew.ToMM(sl.GetThickness()), 4)
            except Exception:
                pass
            for method, key in [("GetEpsilonR", "epsilon_r"), ("GetLossTangent", "loss_tangent")]:
                try:
                    entry[key] = getattr(sl, method)()
                except Exception:
                    pass
            layers.append(entry)
        return {"layers": layers, "source": "stackup_descriptor"}
    except Exception:
        pass

    # Fallback: enumerate copper layers using KiCad's actual layer ID constants.
    # KiCad copper layer IDs are NOT sequential: F.Cu=0, B.Cu=31, inner=1..30.
    # Using range(GetCopperLayerCount()) gives wrong IDs (e.g. 1 → "F.Mask").
    try:
        n = board.GetCopperLayerCount()
        # Build the ordered list: F.Cu, In1..In(n-2), B.Cu
        f_cu = getattr(pcbnew, "F_Cu", 0)
        b_cu = getattr(pcbnew, "B_Cu", 31)
        copper_ids = [f_cu]
        for i in range(1, n - 1):
            copper_ids.append(i)      # In1_Cu=1, In2_Cu=2, ... (stable in all versions)
        if n > 1:
            copper_ids.append(b_cu)
        for lid in copper_ids:
            name = board.GetLayerName(lid)
            layers.append({"name": name, "type": "copper"})
        return {"layers": layers, "source": "copper_layer_enum"}
    except Exception as exc:
        return {"layers": [], "error": str(exc)}


def _handle_get_active_project(_path: str) -> dict[str, Any]:
    import pcbnew
    board = pcbnew.GetBoard()
    result: dict[str, Any] = {"board_path": None, "project_name": None, "project_path": None}
    if board is None:
        return result
    result["board_path"] = board.GetFileName()
    try:
        result["project_name"] = board.GetProject().GetProjectName()
        result["project_path"] = board.GetProject().GetProjectPath()
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Write handlers — each calls _run_on_main_thread to avoid GUI thread crashes
# ---------------------------------------------------------------------------

def _handle_place_component(path: str, reference: str, footprint: str,
                             x: float, y: float, layer: str = "F.Cu",
                             rotation: float = 0.0) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        fp = _load_footprint(footprint)
        fp.SetReference(reference)
        fp.SetPosition(pcbnew.VECTOR2I(_mm(x), _mm(y)))
        fp.SetLayer(_layer_id(board, layer))
        fp.SetOrientationDegrees(rotation)
        board.Add(fp)
        _save_and_refresh(board)
        return {"status": "ok", "reference": reference, "footprint": footprint,
                "x": x, "y": y, "layer": layer, "rotation": rotation}
    return _run_on_main_thread(_do)


def _handle_place_components_bulk(path: str, components: list) -> dict[str, Any]:
    """Place multiple components in one wx main-thread dispatch, then save once."""
    def _do():
        import pcbnew
        board = _get_open_board(path)
        placed = []
        failed = []
        for comp in components:
            reference = comp.get("reference", "")
            footprint = comp.get("footprint", "")
            x = float(comp.get("x", 0.0))
            y = float(comp.get("y", 0.0))
            layer = comp.get("layer", "F.Cu")
            rotation = float(comp.get("rotation", 0.0))
            if not reference or not footprint:
                failed.append({"reference": reference, "reason": "missing reference or footprint"})
                continue
            try:
                fp = _load_footprint(footprint)
                fp.SetReference(reference)
                fp.SetPosition(pcbnew.VECTOR2I(_mm(x), _mm(y)))
                fp.SetLayer(_layer_id(board, layer))
                fp.SetOrientationDegrees(rotation)
                board.Add(fp)
                placed.append(reference)
            except Exception as exc:
                failed.append({"reference": reference, "reason": str(exc)})
        _save_and_refresh(board)
        return {"placed": placed, "failed": failed}
    return _run_on_main_thread(_do)


def _handle_move_component(path: str, reference: str,
                            x: float, y: float,
                            rotation: float | None = None) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        for fp in board.GetFootprints():
            if fp.GetReference() == reference:
                fp.SetPosition(pcbnew.VECTOR2I(_mm(x), _mm(y)))
                if rotation is not None:
                    fp.SetOrientationDegrees(rotation)
                _save_and_refresh(board)
                return {
                    "status": "ok", "reference": reference, "x": x, "y": y,
                    "rotation": rotation if rotation is not None else fp.GetOrientationDegrees(),
                }
        raise ValueError(f"Component {reference!r} not found on board")
    return _run_on_main_thread(_do)


def _handle_add_track(path: str, start_x: float, start_y: float,
                      end_x: float, end_y: float, width: float,
                      layer: str = "F.Cu", net: str = "") -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(_mm(start_x), _mm(start_y)))
        track.SetEnd(pcbnew.VECTOR2I(_mm(end_x), _mm(end_y)))
        track.SetWidth(_mm(width))
        track.SetLayer(_layer_id(board, layer))
        if net:
            netinfo = board.FindNet(net)
            if netinfo:
                track.SetNet(netinfo)
        board.Add(track)
        _save_and_refresh(board)
        return {"status": "ok", "start_x": start_x, "start_y": start_y,
                "end_x": end_x, "end_y": end_y, "width": width, "layer": layer, "net": net}
    return _run_on_main_thread(_do)


def _handle_add_via(path: str, x: float, y: float,
                    size: float = 0.8, drill: float = 0.4,
                    net: str = "", via_type: str = "through") -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(_mm(x), _mm(y)))
        via.SetWidth(_mm(size))
        via.SetDrill(_mm(drill))
        _VIA_TYPES = {
            "through": pcbnew.VIATYPE_THROUGH,
            "blind": pcbnew.VIATYPE_BLIND_BURIED,
            "buried": pcbnew.VIATYPE_BLIND_BURIED,
            "microvia": pcbnew.VIATYPE_MICROVIA,
        }
        via.SetViaType(_VIA_TYPES.get(via_type, pcbnew.VIATYPE_THROUGH))
        if net:
            netinfo = board.FindNet(net)
            if netinfo:
                via.SetNet(netinfo)
        board.Add(via)
        _save_and_refresh(board)
        return {"status": "ok", "x": x, "y": y, "size": size, "drill": drill,
                "net": net, "via_type": via_type}
    return _run_on_main_thread(_do)


def _handle_assign_net(path: str, reference: str, pad: str, net: str) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        netinfo = board.FindNet(net)
        if netinfo is None:
            # Net doesn't exist yet (common on fresh boards) — create it
            netinfo = pcbnew.NETINFO_ITEM(board, net)
            board.Add(netinfo)
        for fp in board.GetFootprints():
            if fp.GetReference() == reference:
                for p in fp.Pads():
                    if p.GetNumber() == pad:
                        p.SetNet(netinfo)
                        _save_and_refresh(board)
                        return {"status": "ok", "reference": reference, "pad": pad, "net": net}
                raise ValueError(f"Pad {pad!r} not found on {reference!r}")
        raise ValueError(f"Component {reference!r} not found on board")
    return _run_on_main_thread(_do)


def _handle_refill_zones(path: str) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        zones = board.Zones()
        zone_count = len(zones) if hasattr(zones, "__len__") else zones.GetCount()
        if zone_count > 0:
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(zones)
        _save_and_refresh(board)
        return {"status": "ok", "zones_filled": zone_count}
    return _run_on_main_thread(_do)


def _handle_add_board_outline(
    path: str, x: float, y: float,
    width: float, height: float, line_width: float = 0.05,
) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        # Idempotency: remove existing Edge.Cuts rectangles before adding
        to_remove = [
            item for item in board.GetDrawings()
            if item.GetLayer() == pcbnew.Edge_Cuts
        ]
        for item in to_remove:
            board.Remove(item)
        rect = pcbnew.PCB_SHAPE(board)
        rect.SetShape(pcbnew.SHAPE_T_RECT)
        rect.SetLayer(pcbnew.Edge_Cuts)
        rect.SetStart(pcbnew.VECTOR2I(_mm(x), _mm(y)))
        rect.SetEnd(pcbnew.VECTOR2I(_mm(x + width), _mm(y + height)))
        rect.SetWidth(_mm(line_width))
        board.Add(rect)
        _save_and_refresh(board)
        return {
            "success": True,
            "x": x, "y": y,
            "width": width, "height": height,
            "x2": round(x + width, 4), "y2": round(y + height, 4),
        }
    return _run_on_main_thread(_do)


def _handle_auto_place(
    path: str, board_x: float, board_y: float,
    board_width: float, board_height: float, clearance_mm: float = 1.5,
) -> dict[str, Any]:
    def _do():
        import pcbnew
        board = _get_open_board(path)
        fps = []
        for fp in board.GetFootprints():
            bbox = fp.GetBoundingBox(False, False)
            fps.append({
                "fp": fp,
                "ref": fp.GetReference(),
                "w": pcbnew.ToMM(bbox.GetWidth()),
                "h": pcbnew.ToMM(bbox.GetHeight()),
            })
        # Sort: connectors first, then ICs, then discretes, then transistors, LEDs, other
        order = {"J": 0, "P": 0, "U": 1, "R": 2, "C": 2, "L": 2, "Q": 3, "T": 3, "D": 4}
        fps.sort(key=lambda i: order.get(
            "".join(c for c in i["ref"] if c.isalpha()).upper()[:2], 5))
        placed: list[str] = []
        warnings: list[str] = []
        cx = board_x + clearance_mm
        cy = board_y + clearance_mm
        row_h = 0.0
        right = board_x + board_width - clearance_mm
        bottom = board_y + board_height - clearance_mm
        for item in fps:
            w = item["w"] + clearance_mm
            h = item["h"] + clearance_mm
            if cx + w > right:
                cx = board_x + clearance_mm
                cy += row_h + clearance_mm
                row_h = 0.0
            if cy + h > bottom:
                warnings.append(f"{item['ref']}: no space")
                continue
            item["fp"].SetPosition(pcbnew.VECTOR2I(_mm(cx + item["w"] / 2), _mm(cy + item["h"] / 2)))
            cx += w
            row_h = max(row_h, h)
            placed.append(item["ref"])
        _save_and_refresh(board)
        return {"placed": placed, "warnings": warnings, "count": len(placed)}
    return _run_on_main_thread(_do)


def _handle_save_board(path: str) -> dict[str, Any]:
    def _do():
        board = _get_open_board(path)
        if not board.GetFileName():
            raise ValueError("Board has no file name; save it manually first")
        _save_and_refresh(board)
        return {"success": True, "path": board.GetFileName()}
    return _run_on_main_thread(_do)


def _handle_export_dsn(path: str, dsn_path: str) -> dict[str, Any]:
    """Export the live in-memory board to a Specctra DSN file.

    Reads from pcbnew's in-memory board, avoiding the external pcbnew.LoadBoard
    that can see stale on-disk content when KiCad has unsaved writes buffered.
    """
    def _do():
        import pcbnew
        board = _get_open_board(path)
        ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
        if not ok:
            raise ValueError(f"ExportSpecctraDSN failed for {dsn_path!r}")
        return {"success": True, "dsn_path": dsn_path}
    return _run_on_main_thread(_do)


def _handle_import_ses(path: str, ses_path: str) -> dict[str, Any]:
    """Import a Specctra SES routing file into the live in-memory board.

    Imports directly into pcbnew's in-memory board and saves via _save_and_refresh,
    so subsequent bridge reads immediately reflect the routed tracks.
    """
    def _do():
        import pcbnew
        board = _get_open_board(path)
        tracks_before = len(board.GetTracks())
        ok = pcbnew.ImportSpecctraSES(board, ses_path)
        if not ok:
            raise ValueError(f"ImportSpecctraSES failed for {ses_path!r}")
        tracks_after = len(board.GetTracks())
        _save_and_refresh(board)
        return {
            "success": True,
            "tracks_before": tracks_before,
            "tracks_after": tracks_after,
            "new_tracks": tracks_after - tracks_before,
        }
    return _run_on_main_thread(_do)


def _handle_reload_board(path: str) -> dict[str, Any]:
    """Reload the board from disk into pcbnew, then refresh the GUI.

    Attempts board.Load(filename) so that post-FreeRouting tracks written to
    the .kicad_pcb file are visible to subsequent bridge reads.  If Load fails
    (KiCad 9 embedded Python quirk), falls back to Refresh()-only so the GUI
    stays consistent.
    """
    def _do():
        import pcbnew
        board = _get_open_board(path)
        filename = board.GetFileName()
        loaded = False
        try:
            board.Load(filename)
            loaded = True
        except Exception as exc:
            logger.warning("reload_board: board.Load() failed (%s); falling back to Refresh()", exc)
        try:
            import wx
            wx.CallAfter(pcbnew.Refresh)
        except Exception:
            pass
        return {"success": True, "path": filename, "loaded": loaded}
    return _run_on_main_thread(_do)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_DISPATCH = {
    "ping":               lambda req: _handle_ping(),
    "get_board_info":     lambda req: _handle_get_board_info(req["path"]),
    "get_components":     lambda req: _handle_get_components(req["path"]),
    "get_nets":           lambda req: _handle_get_nets(req["path"]),
    "get_tracks":         lambda req: _handle_get_tracks(req["path"]),
    "get_design_rules":   lambda req: _handle_get_design_rules(req["path"]),
    "get_stackup":        lambda req: _handle_get_stackup(req["path"]),
    "get_active_project": lambda req: _handle_get_active_project(req.get("path", "")),
    "place_component":    lambda req: _handle_place_component(
        req["path"], req["reference"], req["footprint"],
        req["x"], req["y"], req.get("layer", "F.Cu"), req.get("rotation", 0.0),
    ),
    "place_components_bulk": lambda req: _handle_place_components_bulk(
        req["path"], req.get("components", []),
    ),
    "move_component":     lambda req: _handle_move_component(
        req["path"], req["reference"], req["x"], req["y"], req.get("rotation"),
    ),
    "add_track":          lambda req: _handle_add_track(
        req["path"], req["start_x"], req["start_y"], req["end_x"], req["end_y"],
        req["width"], req.get("layer", "F.Cu"), req.get("net", ""),
    ),
    "add_via":            lambda req: _handle_add_via(
        req["path"], req["x"], req["y"],
        req.get("size", 0.8), req.get("drill", 0.4),
        req.get("net", ""), req.get("via_type", "through"),
    ),
    "assign_net":         lambda req: _handle_assign_net(
        req["path"], req["reference"], req["pad"], req["net"],
    ),
    "refill_zones":       lambda req: _handle_refill_zones(req["path"]),
    "save_board":         lambda req: _handle_save_board(req["path"]),
    "reload_board":       lambda req: _handle_reload_board(req["path"]),
    "add_board_outline":  lambda req: _handle_add_board_outline(
        req["path"], req["x"], req["y"], req["width"], req["height"],
        req.get("line_width", 0.05),
    ),
    "auto_place":         lambda req: _handle_auto_place(
        req["path"], req["board_x"], req["board_y"],
        req["board_width"], req["board_height"], req.get("clearance_mm", 1.5),
    ),
    "export_dsn":         lambda req: _handle_export_dsn(req["path"], req["dsn_path"]),
    "import_ses":         lambda req: _handle_import_ses(req["path"], req["ses_path"]),
}


# ---------------------------------------------------------------------------
# TCP request handler
# ---------------------------------------------------------------------------

class _MCPRequestHandler(socketserver.StreamRequestHandler):
    """Handles one newline-delimited JSON request/response cycle."""

    def handle(self):
        try:
            raw = self.rfile.readline()
            if not raw:
                return
            request = json.loads(raw.decode("utf-8").strip())
            response = self._dispatch(request)
        except Exception as exc:
            response = {"status": "error", "message": str(exc)}
        try:
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def _dispatch(self, request: dict) -> dict:
        method = request.get("method", "")
        handler = _DISPATCH.get(method)
        if handler is None:
            return {"status": "error", "message": f"Unknown method: {method!r}"}
        try:
            result = handler(request)
            return {"status": "ok", "result": result}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _start_server() -> None:
    """Start the TCP server in a daemon background thread."""
    global _tcp_server, _server_thread

    try:
        socketserver.TCPServer.allow_reuse_address = True
        _tcp_server = socketserver.TCPServer(("localhost", _PORT), _MCPRequestHandler)
    except Exception as exc:
        logger.warning("KiCad MCP bridge: could not bind to port %d: %s", _PORT, exc)
        _write_diag(f"_start_server BIND FAILED: {type(exc).__name__}: {exc}")
        return

    def _serve():
        try:
            _tcp_server.serve_forever()
        except Exception as exc:
            _write_diag(f"serve_forever EXITED unexpectedly: {type(exc).__name__}: {exc}")

    _server_thread = threading.Thread(
        target=_serve,
        name="kicad-mcp-bridge",
        daemon=True,
    )
    _server_thread.start()
    logger.info("KiCad MCP bridge listening on localhost:%d", _PORT)


# ---------------------------------------------------------------------------
# KiCad ActionPlugin interface
# KiCad loads plugins by importing this module — self-registration must happen
# at module level. KiCad does NOT call a register() function.
#
# IMPORTANT: _start_server() is called unconditionally whenever pcbnew is
# importable (i.e. we are inside KiCad).  ActionPlugin.register() is a
# best-effort step that makes the plugin visible in the plugin manager; a
# failure there must NOT prevent the TCP server from starting.
# ---------------------------------------------------------------------------

def _write_diag(msg: str) -> None:
    """Write a timestamped diagnostic line to the bridge startup log."""
    import datetime
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_startup.log")
        with open(log_path, "a", encoding="utf-8") as _f:
            _f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


_write_diag(f"MODULE IMPORTED from {__file__}")

try:
    import pcbnew
    _write_diag(f"pcbnew import SUCCESS — version: {pcbnew.GetBuildVersion()}")
except ImportError as _imp_err:
    _write_diag(f"pcbnew import FAILED ({_imp_err}) — server will NOT start; register() exposed for manual use")
    # Outside KiCad (unit tests with mock server) — expose register() for
    # manual startup so tests can start the server explicitly.
    def register():
        _start_server()
else:
    # Inside KiCad — start the TCP server unconditionally.
    _start_server()
    if _tcp_server is not None:
        _write_diag(f"TCP server started on port {_PORT}")
    else:
        _write_diag(f"TCP server FAILED to bind on port {_PORT}")

    # Register as an ActionPlugin so KiCad records the plugin in pcbnew.json
    # (action_plugins list) and auto-loads it on subsequent sessions.
    # This is a best-effort step; failure here must not kill the server.
    try:
        class KiCadMCPBridge(pcbnew.ActionPlugin):
            """ActionPlugin wrapper — TCP server starts at module load time."""

            def defaults(self):
                self.name = "KiCad MCP Bridge"
                self.category = "MCP"
                self.description = (
                    "Exposes a local TCP API so the KiCad MCP server can read "
                    "and modify the open board directly via pcbnew — no gRPC."
                )
                self.show_toolbar_button = False

            def Run(self):
                pass  # Server already running; nothing to do.

        KiCadMCPBridge().register()
        _write_diag("ActionPlugin registered with KiCad")
        logger.info("KiCad MCP bridge: ActionPlugin registered")
    except Exception as exc:
        _write_diag(f"ActionPlugin registration failed ({exc}) — TCP server still running")
        logger.warning(
            "KiCad MCP bridge: ActionPlugin registration failed (%s). "
            "TCP server is still running on port %d.",
            exc, _PORT,
        )
