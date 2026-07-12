"""IPC board backend — kipy-based ``BoardOps`` over the KiCad IPC API (F1).

Primary live-board path (spec §2.2, bridge-board-access): a drop-in ``BoardOps``
provider that the router slots ahead of the SWIG-bridge ``PluginBoardOps``.
Return shapes match the bridge handlers exactly (REQ-COV-2) so the tool layer
cannot tell which live path served it.

S1 scope (spec §3 rows 1–12): reads (read_board / get_board_info /
get_components / get_nets / get_tracks / get_active_project) and the core
writes (place_component / move_component / add_track / add_via / assign_net /
add_board_outline / clear_routes), each an atomic ``_commit`` transaction
followed by a disk save. S2 scope (rows 13–21): the specialized ops —
get_stackup / get_design_rules / refill_zones / auto_place / save_board plus
project text variables (rows 20–21, served from ``IPCBackend``). Row 15
(``set_board_design_rules``) stays off IPC by design: kipy 0.5.0 has no
netclass/design-rules write wrapper, and the tool intentionally edits the
.kicad_pro file-side (pcbnew-clobber contract, d018367). Methods not covered
keep the ``BoardOps`` base default (``NotImplementedError``) so the router
falls through to the bridge (REQ-ROUTE-4) — never a stubbed wrong result;
writes whose server-side IPC support is unverified validate the result
in-commit and use the same signal.

All distances cross the IPC boundary in nanometers (kipy convention); the MCP
surface stays in millimeters, rounded to 4 decimals like the bridge.
"""

from __future__ import annotations

import os
import shutil
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from kicad_mcp.backends.base import BackendCapability, BoardOps, KiCadBackend
from kicad_mcp.backends.ipc_connection import IPCConnection, IPCUnavailableError
from kicad_mcp.backends.placement_guard import (
    check_placement,
    idempotent_success,
    index_existing,
)
from kicad_mcp.logging_config import get_logger

if TYPE_CHECKING:
    from kipy.board import Board
    from kipy.board_types import FootprintInstance, Net
    from kipy.common_types import Commit

try:
    import kipy.board_types as kbt
    from kipy.common_types import LibraryIdentifier
    from kipy.geometry import Angle, Vector2
    from kipy.project_types import TextVariables
    from kipy.proto.board.board_pb2 import BoardStackupLayerType
    from kipy.proto.board.board_types_pb2 import BoardLayer, ViaType
    from kipy.proto.common.types import MapMergeMode
    from kipy.proto.common.types import project_settings_pb2
    from kipy.util.board_layer import canonical_name, layer_from_canonical_name
    from kipy.util.units import from_mm, to_mm
except ImportError:  # REQ-IPC-8: missing client degrades, never crashes
    kbt = None  # type: ignore[assignment]
    LibraryIdentifier = None  # type: ignore[assignment,misc]
    Angle = None  # type: ignore[assignment,misc]
    Vector2 = None  # type: ignore[assignment,misc]
    TextVariables = None  # type: ignore[assignment,misc]
    BoardStackupLayerType = None  # type: ignore[assignment,misc]
    BoardLayer = None  # type: ignore[assignment,misc]
    ViaType = None  # type: ignore[assignment,misc]
    MapMergeMode = None  # type: ignore[assignment,misc]
    project_settings_pb2 = None  # type: ignore[assignment]
    canonical_name = None  # type: ignore[assignment]
    layer_from_canonical_name = None  # type: ignore[assignment]
    from_mm = None  # type: ignore[assignment]
    to_mm = None  # type: ignore[assignment]

logger = get_logger("backend.ipc")

_T = TypeVar("_T")


def _mm4(value_nm: int) -> float:
    """Nanometers → millimeters rounded to 4 decimals (bridge parity)."""
    return round(to_mm(value_nm), 4)


def _save_board(board: Board) -> None:
    # kipy 0.5.0 ships py.typed but leaves Board.save() unannotated
    board.save()  # type: ignore[no-untyped-call]


def _bridge_save(path: Path) -> bool:
    """Best-effort save through the bridge; True when the bridge saved.

    In a mixed IPC/bridge session the bridge keeps a stale-board mtime
    baseline (#14C) that only ITS OWN saves update — an IPC-side save would
    advance the disk mtime behind its back and every later bridge mutation
    would be refused as stale (live-caught in the S1 step-7 batch). So the
    post-commit flush prefers the bridge whenever it is reachable; both paths
    save the same live in-memory board.
    """
    try:
        from kicad_mcp.backends.plugin_backend import _get_op_timeout, _tcp_call
        _tcp_call("save_board", _get_op_timeout(), path=str(path))
        return True
    except Exception:  # noqa: BLE001 — bridge down/refused: IPC-only session
        return False


class IPCBoardOps(BoardOps):
    """Board operations served by the KiCad IPC API via kipy."""

    def __init__(self, connection: IPCConnection) -> None:
        self._conn = connection

    # -- Board resolution ------------------------------------------------------

    def _board(self, path: Path) -> Board:
        """Resolve the open board and verify it is the one ``path`` names.

        IPC has no "open board" call (C1b) — it serves whatever document KiCad
        already has open. ``Board.name`` is the bare filename (live-verified),
        so the match is by filename; a mismatch raises with the canonical
        "does not match open board" phrase the recovery guidance keys on.
        """
        board = self._conn.board()
        if os.path.normcase(Path(path).name) != os.path.normcase(board.name):
            raise IPCUnavailableError(
                f"Requested board '{Path(path).name}' does not match open board "
                f"'{board.name}'",
                remedy="Call open_kicad with the correct .kicad_pcb path and wait "
                       "for the board to load, then retry.",
            )
        return board

    # -- Transactional write pattern (REQ-IPC-4, REQ-IPC-6) --------------------

    def _commit(self, board: Board, mutate: Callable[[Board, Commit], _T]) -> _T:
        """Run ``mutate`` inside one commit; atomic apply or full drop.

        On any error mid-commit the transaction is dropped so the live board is
        left unchanged (REQ-IPC-6) — there is no partial-write state. The
        original exception propagates for the router/tool boundary to reshape
        (REQ-SAFE-4).
        """
        commit = board.begin_commit()
        try:
            result = mutate(board, commit)
        except Exception:
            try:
                board.drop_commit(commit)
            except Exception:  # noqa: BLE001 — dropping a dead commit is best-effort
                logger.warning("drop_commit failed after mutate error", exc_info=True)
            raise
        board.push_commit(commit)
        return result

    def _write(self, path: Path, mutate: Callable[[Board, Commit], _T]) -> _T:
        """Resolve + verify the board, run one atomic commit, save to disk.

        The post-commit save mirrors the bridge's _save_and_refresh: the hybrid
        architecture has file-based tools (courtyard, orientation, quality)
        reading the .kicad_pcb, so every live write must land on disk too.
        """
        board = self._board(path)
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    def _save(self, path: Path, board: Board) -> None:
        """Flush the live board to disk — bridge-first (see _bridge_save)."""
        if not _bridge_save(path):
            _save_board(board)

    # -- kipy lookup helpers -----------------------------------------------------

    @staticmethod
    def _vec(x_mm: float, y_mm: float) -> Vector2:
        return Vector2.from_xy(from_mm(x_mm), from_mm(y_mm))

    @staticmethod
    def _find_footprint(board: Board, reference: str) -> FootprintInstance:
        for fp in board.get_footprints():
            if fp.reference_field.text.value == reference:
                return fp
        # message parity with the bridge handlers
        raise ValueError(f"Component {reference!r} not found on board")

    @staticmethod
    def _find_net(board: Board, net_name: str) -> Net | None:
        for net in board.get_nets():
            if net.name == net_name:
                return net
        return None

    # -- Core writes (spec §3 rows 5–12) ----------------------------------------
    #
    # Live-verified against KiCad 9.0.7 IPC (2026-07-10, 18/18 e2e checks,
    # each confirmed by re-reading the live board): move / add_track / add_via /
    # assign_net(existing net) / add_board_outline / clear_routes are fully
    # IPC-served. Two server gaps found: create-by-lib-id yields an EMPTY
    # footprint instance (0 pads even after push), and create_items(Net) is
    # unsupported (absent after push; kipy can't unpack the response either).
    # The in-commit validation below catches both and raises
    # NotImplementedError, which drops the commit (board untouched — verified:
    # no ghost footprint remains) and is the router's fall-through-to-bridge
    # signal (REQ-ROUTE-4). The guards stay adaptive: if a future KiCad adds
    # server support, these ops start serving over IPC with no code change.

    def place_component(
        self, path: Path, reference: str, footprint: str,
        x: float, y: float, layer: str = "F.Cu", rotation: float = 0.0,
    ) -> dict[str, Any]:
        # Duplicate-ref guard (#16, REQ-DUP-1..3) BEFORE the create attempt,
        # so idempotent hits and refusals are served here instead of falling
        # through to the bridge (whose client runs the same shared rule).
        existing = index_existing(self.get_components(path)).get(reference)
        if check_placement(existing, reference, footprint,
                           x, y, rotation, layer) == "idempotent":
            assert existing is not None
            return idempotent_success(existing)

        def mutate(board: Board, commit: Commit) -> dict[str, Any]:
            fp = kbt.FootprintInstance()
            lib, _, name = footprint.partition(":")
            lib_id = LibraryIdentifier()
            lib_id.library = lib
            lib_id.name = name
            fp.definition.id = lib_id
            fp.reference_field.text.value = reference
            fp.position = self._vec(x, y)
            fp.layer = layer_from_canonical_name(layer)
            fp.orientation = Angle.from_degrees(rotation)
            created = board.create_items(fp)
            item = created[0] if created else None
            # The server must resolve the library id into a real definition.
            # KiCad 9.0.7 does not (live-verified: instance lands with 0 pads/
            # 0 items even after push), so this guard currently always routes
            # place_component to the bridge; the response echo is authoritative
            # (echo pads == live pads). Graphics-only footprints also land here
            # — the bridge handles both correctly.
            if not isinstance(item, kbt.FootprintInstance) or not item.definition.pads:
                raise NotImplementedError(
                    f"IPC create_items did not materialize footprint {footprint!r} "
                    "from its library id — serve via the bridge path"
                )
            return {"status": "ok", "reference": reference, "footprint": footprint,
                    "x": x, "y": y, "layer": layer, "rotation": rotation}
        return self._write(path, mutate)

    def move_component(
        self, path: Path, reference: str, x: float, y: float,
        rotation: float | None = None,
    ) -> dict[str, Any]:
        board = self._board(path)
        fp = self._find_footprint(board, reference)

        def mutate(board_: Board, commit: Commit) -> dict[str, Any]:
            fp.position = self._vec(x, y)
            if rotation is not None:
                fp.orientation = Angle.from_degrees(rotation)
            board_.update_items(fp)
            return {
                "status": "ok", "reference": reference, "x": x, "y": y,
                "rotation": rotation if rotation is not None else fp.orientation.degrees,
            }
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    def add_track(
        self, path: Path, start_x: float, start_y: float,
        end_x: float, end_y: float, width: float,
        layer: str = "F.Cu", net: str = "",
    ) -> dict[str, Any]:
        def mutate(board: Board, commit: Commit) -> dict[str, Any]:
            track = kbt.Track()
            track.start = self._vec(start_x, start_y)
            track.end = self._vec(end_x, end_y)
            track.width = from_mm(width)
            track.layer = layer_from_canonical_name(layer)
            if net:
                # bridge parity: an unknown net name is silently skipped
                net_obj = self._find_net(board, net)
                if net_obj is not None:
                    track.net = net_obj
            if not board.create_items(track):
                raise RuntimeError("IPC create_items returned no created track")
            return {"status": "ok", "start_x": start_x, "start_y": start_y,
                    "end_x": end_x, "end_y": end_y, "width": width,
                    "layer": layer, "net": net}
        return self._write(path, mutate)

    def add_via(
        self, path: Path, x: float, y: float,
        size: float = 0.8, drill: float = 0.4,
        net: str = "", via_type: str = "through",
    ) -> dict[str, Any]:
        def mutate(board: Board, commit: Commit) -> dict[str, Any]:
            via = kbt.Via()
            via.type = {
                "through": ViaType.VT_THROUGH,
                "blind": ViaType.VT_BLIND_BURIED,
                "buried": ViaType.VT_BLIND_BURIED,
                "microvia": ViaType.VT_MICRO,
            }.get(via_type, ViaType.VT_THROUGH)
            via.position = self._vec(x, y)
            via.diameter = from_mm(size)
            via.drill_diameter = from_mm(drill)
            if net:
                net_obj = self._find_net(board, net)
                if net_obj is not None:
                    via.net = net_obj
            if not board.create_items(via):
                raise RuntimeError("IPC create_items returned no created via")
            return {"status": "ok", "x": x, "y": y, "size": size, "drill": drill,
                    "net": net, "via_type": via_type}
        return self._write(path, mutate)

    def assign_net(
        self, path: Path, reference: str, pad: str, net: str,
    ) -> dict[str, Any]:
        board = self._board(path)
        fp = self._find_footprint(board, reference)
        existing_net = self._find_net(board, net)

        def mutate(board_: Board, commit: Commit) -> dict[str, Any]:
            target_net = existing_net
            if target_net is None:
                # Bridge parity: a missing net is created on the fly. KiCad
                # 9.0.7 IPC cannot create nets (live-verified: absent after
                # push), so this path currently always falls back to the
                # bridge; kept adaptive for future server support.
                new_net = kbt.Net()
                new_net.name = net
                try:
                    created = board_.create_items(new_net)
                except Exception as exc:  # noqa: BLE001 — server refused the item kind
                    raise NotImplementedError(
                        f"IPC cannot create net {net!r} — serve via the bridge path"
                    ) from exc
                created_net = created[0] if created else None
                if not isinstance(created_net, kbt.Net) or created_net.name != net:
                    raise NotImplementedError(
                        f"IPC cannot create net {net!r} — serve via the bridge path"
                    )
                target_net = created_net
            # Update ALL pads with this number (multi-pad thermal arrays —
            # same contract as the bridge and FileBoardOps).
            pads_updated = 0
            for item in fp.definition.pads:
                if item.number == pad:
                    item.net = target_net
                    pads_updated += 1
            if pads_updated == 0:
                raise ValueError(f"Pad {pad!r} not found on {reference!r}")
            updated = board_.update_items(fp)
            # Live-verified: pad nets applied through a footprint update DO
            # land (re-read after push confirms), and the update echo matches
            # the post-push board — this check is cheap insurance that keeps
            # us honest if a future server stops applying pad nets.
            applied = any(
                item.number == pad and item.net.name == net
                for u in updated if isinstance(u, kbt.FootprintInstance)
                for item in u.definition.pads
            )
            if not applied:
                raise NotImplementedError(
                    "IPC update_items did not apply the pad net — serve via the "
                    "bridge path"
                )
            return {"status": "ok", "reference": reference, "pad": pad,
                    "net": net, "pads_updated": pads_updated}
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    def add_board_outline(
        self, path: Path, x: float, y: float,
        width: float, height: float, line_width: float = 0.05,
    ) -> dict[str, Any]:
        board = self._board(path)
        # Idempotency (bridge parity): existing Edge.Cuts shapes are replaced,
        # atomically in the same commit as the new outline.
        stale_edges = [s for s in board.get_shapes()
                       if s.layer == BoardLayer.BL_Edge_Cuts]

        def mutate(board_: Board, commit: Commit) -> dict[str, Any]:
            if stale_edges:
                board_.remove_items(stale_edges)
            rect = kbt.BoardRectangle()
            rect.layer = BoardLayer.BL_Edge_Cuts
            rect.top_left = self._vec(x, y)
            rect.bottom_right = self._vec(x + width, y + height)
            rect.attributes.stroke.width = from_mm(line_width)
            if not board_.create_items(rect):
                raise RuntimeError("IPC create_items returned no created outline")
            return {
                "success": True,
                "x": x, "y": y,
                "width": width, "height": height,
                "x2": round(x + width, 4), "y2": round(y + height, 4),
            }
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    def clear_routes(self, path: Path, backup: bool = True) -> dict[str, Any]:
        board = self._board(path)
        backup_path: str | None = None
        if backup:
            # Flush live state first so the backup matches the pre-clear board
            # (bridge parity), then copy the file aside.
            self._save(path, board)
            filename = str(path)
            if filename.endswith(".kicad_pcb"):
                backup_file = filename[:-len(".kicad_pcb")] + ".clear_routes_backup.kicad_pcb"
            else:
                backup_file = filename + ".clear_routes_backup"
            shutil.copy2(filename, backup_file)
            backup_path = backup_file

        tracks = list(board.get_tracks())  # Track + ArcTrack
        vias = list(board.get_vias())

        def mutate(board_: Board, commit: Commit) -> dict[str, Any]:
            doomed: list[Any] = [*tracks, *vias]
            if doomed:
                board_.remove_items(doomed)
            return {
                "status": "success",
                "tracks_removed": len(tracks),
                "vias_removed": len(vias),
                "backup_path": backup_path,
            }
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    # -- Specialized ops (spec §3 rows 16–19, S2) --------------------------------

    def refill_zones(self, path: Path) -> dict[str, Any]:
        """Spec §3 row 16 — ``board.refill_zones()`` (kipy blocks until done)."""
        board = self._board(path)
        zones = board.get_zones()
        if zones:
            board.refill_zones()
        self._save(path, board)
        return {"status": "ok", "zones_filled": len(zones)}

    def save_board(self, path: Path) -> dict[str, Any]:
        """Spec §3 row 19 — flush the live board to disk (bridge-first save)."""
        board = self._board(path)
        self._save(path, board)
        return {"success": True, "path": str(path)}

    def clean_board_for_routing(
        self, path: Path,
        remove_keepouts: bool = True,
        remove_unassigned_tracks: bool = True,
    ) -> dict[str, Any]:
        """Spec §3 row 18 — routing prep on the LIVE board.

        Removes rule-area (keep-out) zones and net-less tracks/vias in one
        atomic commit. Serving this over IPC replaces the headless disk-side
        pcbnew script for live sessions, which wrote the .kicad_pcb behind
        pcbnew's back (#14C hazard). Net-less vias count as tracks for parity
        with the SWIG path (``board.GetTracks()`` includes vias).
        """
        board = self._board(path)
        doomed_zones = (
            [z for z in board.get_zones() if z.is_rule_area()]
            if remove_keepouts else []
        )
        doomed_tracks = (
            [t for t in [*board.get_tracks(), *board.get_vias()] if not t.net.name]
            if remove_unassigned_tracks else []
        )

        def mutate(board_: Board, commit: Commit) -> dict[str, Any]:
            doomed: list[Any] = [*doomed_zones, *doomed_tracks]
            if doomed:
                board_.remove_items(doomed)
            return {
                "status": "success",
                "keepouts_removed": len(doomed_zones),
                "tracks_removed": len(doomed_tracks),
            }
        result = self._commit(board, mutate)
        self._save(path, board)
        return result

    def auto_place(
        self, path: Path, board_x: float, board_y: float,
        board_width: float, board_height: float, clearance_mm: float = 1.5,
        anchors: list[str] | None = None,
        strategy: str = "net_aware",
    ) -> dict[str, Any]:
        """Spec §3 row 17 — net-aware placement over IPC moves.

        The engine is pure Python and reads the on-disk board, exactly like the
        bridge-side net-aware branch: flush live state to disk, compute the
        plan, apply each pose through ``move_component`` (IPC), save. Anchored
        refs are never moved (AC7). The legacy "row" packer runs inside pcbnew
        and stays bridge-served.
        """
        if strategy == "row":
            raise NotImplementedError(
                "legacy row packer runs inside pcbnew — serve via the bridge path"
            )
        from kicad_mcp.backends.file_backend import build_engine_parts
        from kicad_mcp.utils import placement_engine as engine

        board = self._board(path)
        self._save(path, board)  # live board -> disk, so the plan is current

        parts = build_engine_parts(path, path.parent)
        if not parts:
            return {
                "components_placed": 0, "rows": 0, "total_area_mm2": 0.0,
                "placements": [], "warnings": [], "strategy": "net_aware",
            }

        keepouts, part_sides = engine.read_board_keepouts(path)
        items, warnings, total_area = engine.compute_net_aware_plan(
            parts, board_x, board_y, board_width, board_height,
            clearance_mm, anchors,
            diff_pair_nets=engine.read_diff_pair_nets(path),
            keepouts=keepouts, part_sides=part_sides,
        )
        placements: list[dict[str, Any]] = []
        applied_warnings: list[Any] = list(warnings)
        for ref, x, y, rot in items:
            try:
                self.move_component(path, ref, x, y, rotation=rot)
                placements.append({"reference": ref, "x": x, "y": y})
            except Exception as exc:  # noqa: BLE001 — bridge parity: warn, keep going
                applied_warnings.append(f"{ref}: move failed — {exc}")
        self._save(path, board)
        return {
            "components_placed": len(placements),
            "rows": 0,
            "total_area_mm2": total_area,
            "placements": placements,
            "warnings": applied_warnings,
            "strategy": "net_aware",
        }

    # -- Reads (spec §3 rows 1–4) ----------------------------------------------

    def read_board(self, path: Path) -> dict[str, Any]:
        info = self.get_board_info(path)
        components = self.get_components(path)
        nets = self.get_nets(path)
        tracks = self.get_tracks(path)
        return {"info": info, "components": components, "nets": nets, "tracks": tracks}

    def get_board_info(self, path: Path) -> dict[str, Any]:
        board = self._board(path)
        title_block = board.get_title_block_info()
        width_mm, height_mm = self._edge_bbox_mm(board)
        return {
            "title": title_block.title,
            "revision": title_block.revision,
            "layer_count": board.get_copper_layer_count(),
            "width_mm": width_mm,
            "height_mm": height_mm,
            "net_count": len(board.get_nets()),
            "footprint_count": len(board.get_footprints()),
        }

    def _edge_bbox_mm(self, board: Board) -> tuple[float, float]:
        """Board size from the union of Edge.Cuts shape bounding boxes.

        Parity note: the bridge uses pcbnew's GetBoardEdgesBoundingBox, which
        also counts footprint-owned edge shapes; IPC get_shapes() returns
        board-level shapes only. Identical for every normal board outline.
        """
        edges = [s for s in board.get_shapes() if s.layer == BoardLayer.BL_Edge_Cuts]
        if not edges:
            return (0.0, 0.0)
        boxes = [b for b in board.get_item_bounding_box(edges) if b is not None]
        if not boxes:
            return (0.0, 0.0)
        # Union computed here rather than via Box2.merge: an empty Box2 sits at
        # the origin and merging it would wrongly extend the outline to (0, 0).
        x0 = min(b.pos.x for b in boxes)
        y0 = min(b.pos.y for b in boxes)
        x1 = max(b.pos.x + b.size.x for b in boxes)
        y1 = max(b.pos.y + b.size.y for b in boxes)
        return (_mm4(x1 - x0), _mm4(y1 - y0))

    def get_components(self, path: Path) -> list[dict[str, Any]]:
        board = self._board(path)
        components: list[dict[str, Any]] = []
        for fp in board.get_footprints():
            position = fp.position
            components.append({
                "reference": fp.reference_field.text.value,
                "value": fp.value_field.text.value,
                "footprint": str(fp.definition.id),
                "x": _mm4(position.x),
                "y": _mm4(position.y),
                "layer": canonical_name(fp.layer),
                "rotation": round(fp.orientation.degrees, 4),
            })
        return components

    def get_nets(self, path: Path) -> list[dict[str, Any]]:
        board = self._board(path)
        # Net.code is deprecated in kipy (gone in KiCad 10) but the MCP surface
        # shape carries net_id (REQ-COV-2); silence just the accessor until the
        # tool layer drops the field.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return [{"net_id": net.code, "name": net.name} for net in board.get_nets()]

    def get_tracks(self, path: Path) -> list[dict[str, Any]]:
        board = self._board(path)
        items: list[dict[str, Any]] = []
        for track in board.get_tracks():  # Track and ArcTrack both expose start/end/width
            start = track.start
            end = track.end
            items.append({
                "type": "track",
                "start_x": _mm4(start.x),
                "start_y": _mm4(start.y),
                "end_x": _mm4(end.x),
                "end_y": _mm4(end.y),
                "width": _mm4(track.width),
                "layer": canonical_name(track.layer),
                "net": track.net.name,
            })
        for via in board.get_vias():
            position = via.position
            items.append({
                "type": "via",
                "x": _mm4(position.x),
                "y": _mm4(position.y),
                "size": _mm4(via.diameter),
                "drill": _mm4(via.drill_diameter),
                "net": via.net.name,
            })
        return items

    # -- Specialized reads (spec §3 rows 13–14, S2) ------------------------------

    def get_design_rules(self, path: Path) -> dict[str, Any]:
        """Spec §3 row 14 — Default-netclass values over IPC.

        KiCad 9.0.7 IPC exposes project netclasses but not the board-level
        minimum constraints (``m_TrackMinWidth`` family) the bridge also
        reports — those stay a documented bridge extra. An absent or empty
        Default netclass falls through to the bridge rather than returning a
        hollow result.
        """
        board = self._board(path)
        classes = board.get_project().get_net_classes()
        default = next((c for c in classes if c.name == "Default"), None)
        if default is None:
            raise NotImplementedError(
                "IPC returned no Default netclass — serve via the bridge path"
            )
        result: dict[str, Any] = {}
        for attr, key in [
            ("clearance", "clearance_mm"),
            ("track_width", "track_width_mm"),
            ("via_diameter", "via_diameter_mm"),
            ("via_drill", "via_drill_mm"),
        ]:
            value_nm = getattr(default, attr)
            if value_nm is not None:
                result[key] = _mm4(value_nm)
        if not result:
            raise NotImplementedError(
                "IPC Default netclass carries no board settings — serve via "
                "the bridge path"
            )
        return result

    def get_stackup(self, path: Path) -> dict[str, Any]:
        """Spec §3 row 13 — bridge-shape ``{layers, source}`` from kipy.

        Shape parity note: the IPC layer-type enum cannot distinguish core
        from prepreg (both map to "dielectric"); epsilon_r / loss_tangent come
        from the first dielectric sub-layer, matching what the bridge reads
        off the stackup descriptor.
        """
        board = self._board(path)
        type_names = {
            BoardStackupLayerType.BSLT_COPPER: "copper",
            BoardStackupLayerType.BSLT_DIELECTRIC: "dielectric",
            BoardStackupLayerType.BSLT_SILKSCREEN: "silkscreen",
            BoardStackupLayerType.BSLT_SOLDERMASK: "soldermask",
            BoardStackupLayerType.BSLT_SOLDERPASTE: "solderpaste",
        }
        layers: list[dict[str, Any]] = []
        for layer in board.get_stackup().layers:
            entry: dict[str, Any] = {
                "name": layer.user_name,
                "type": type_names.get(layer.type, "unknown"),
                "thickness_mm": _mm4(layer.thickness),
            }
            if layer.material_name:
                entry["material"] = layer.material_name
            if layer.type == BoardStackupLayerType.BSLT_DIELECTRIC:
                sub = layer.dielectric.layers
                if sub:
                    entry["epsilon_r"] = sub[0].epsilon_r
                    entry["loss_tangent"] = sub[0].loss_tangent
                    if "material" not in entry and sub[0].material_name:
                        entry["material"] = sub[0].material_name
            layers.append(entry)
        return {"layers": layers, "source": "stackup_descriptor"}


class IPCBackend(KiCadBackend):
    """KiCad backend serving live-board ops over the IPC API (kipy)."""

    def __init__(self, connection: IPCConnection | None = None) -> None:
        self._conn = connection if connection is not None else IPCConnection()
        self._board_ops = IPCBoardOps(self._conn)

    @property
    def name(self) -> str:
        return "ipc"

    @property
    def capabilities(self) -> set[BackendCapability]:
        # REQ-IPC-7 — exactly the four live-board capability groups.
        return {
            BackendCapability.BOARD_READ,
            BackendCapability.BOARD_MODIFY,
            BackendCapability.ZONE_REFILL,
            BackendCapability.BOARD_STACKUP,
        }

    def is_available(self) -> bool:
        """Real reachability — server answering AND a loaded board open
        (REQ-IPC-7 / REQ-ROUTE-3), never a static True."""
        return self._conn.is_available()

    @property
    def connection(self) -> IPCConnection:
        return self._conn

    def get_board_ops(self) -> IPCBoardOps:
        return self._board_ops

    def get_active_project(self) -> dict[str, Any]:
        """Spec §3 row 3 — bridge-shape {board_path, project_name, project_path}."""
        result: dict[str, Any] = {
            "board_path": None, "project_name": None, "project_path": None,
        }
        board = self._conn.board()  # raises IPCUnavailableError when none open
        result["board_path"] = board.name
        try:
            project = board.get_project()
            result["project_name"] = project.name
            result["project_path"] = project.path
            if project.path:
                # Board.name is the bare filename; qualify it like the bridge's
                # GetFileName() full path. project.path may name the project
                # directory or the .kicad_pro file itself — tolerate both.
                base = Path(project.path)
                if base.suffix:
                    base = base.parent
                result["board_path"] = str(base / board.name)
        except Exception:  # noqa: BLE001 — mirror the bridge: project info is best-effort
            pass
        return result

    # -- Project text variables (spec §3 rows 20–21, S2²→covered) ---------------

    def _project(self, project_path: Path) -> Any:
        """Resolve the open board's project and verify it is the one named.

        Mirrors ``IPCBoardOps._board``: a mismatch raises with the canonical
        "does not match open" phrase so the router falls through cleanly.
        """
        board = self._conn.board()
        project = board.get_project()
        requested = Path(project_path).stem
        if project.name and os.path.normcase(requested) != os.path.normcase(project.name):
            raise IPCUnavailableError(
                f"Requested project '{requested}' does not match open project "
                f"'{project.name}'",
                remedy="Call open_kicad with the correct project path and wait "
                       "for the board to load, then retry.",
            )
        return project

    def get_text_variables(self, project_path: Any) -> dict[str, Any]:
        """Spec §3 row 20 — live text-variable definitions via kipy Project."""
        variables = self._project(Path(project_path)).get_text_variables()
        return {"status": "success", "variables": dict(variables.variables)}

    def set_text_variables(
        self, project_path: Any, variables: dict[str, str],
    ) -> dict[str, Any]:
        """Spec §3 row 21 — replace-semantics write via kipy Project.

        MMM_REPLACE keeps parity with the file-side implementation, which
        overwrites the whole ``text_variables`` map. The post-write save
        flushes live state to disk so file-side readers stay coherent
        (bridge-first, same #14C rationale as ``IPCBoardOps._save``).
        """
        project = self._project(Path(project_path))
        # TextVariables() without an explicit proto would alias the shared
        # default-argument instance in kipy 0.5.0 (mutable-default bug) —
        # always hand it a fresh proto.
        wrapped = TextVariables(project_settings_pb2.TextVariables())
        wrapped.variables = dict(variables)
        project.set_text_variables(wrapped, MapMergeMode.MMM_REPLACE)
        board_path = Path(project_path).with_suffix(".kicad_pcb")
        if not _bridge_save(board_path):
            try:
                _save_board(self._conn.board())
            except Exception:  # noqa: BLE001 — write landed; the flush is best-effort
                logger.warning("post-set_text_variables save failed", exc_info=True)
        return {"status": "success", "variables": variables, "count": len(variables)}
