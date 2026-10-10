"""Routed-track import seam for external routers (TraceMaker Stage 1, Unit 2).

TraceMaker routes a temp COPY of the board and writes a routed ``.kicad_pcb``.
The live bridge (inside pcbnew) holds the real board in memory, and KiCad 9
cannot reload a board in place — so the new copper must be extracted from the
routed copy and re-applied to the live board through the ``add_routed_tracks``
bridge handler.

Safety contract (same as ``import_ses``): a failed import never modifies the
board file. The bridge batch is all-or-nothing; the file fallback only ever
replaces the board file when it is byte-identical (SHA256) to the copy the
router was given.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.sexp_parser import parse_sexp_file

logger = get_logger("tools.routing_import")

# Coordinate/width match precision for the added-routes diff: 4 decimal places
# of a millimetre (0.1 µm). KiCad writes mm with up to 6 decimals at 1 nm
# resolution, so 0.1 µm safely absorbs serialization noise while never merging
# two distinct routed items.
_ROUND_PLACES = 4


def _r(value: float) -> float:
    return round(float(value), _ROUND_PLACES)


# ---------------------------------------------------------------------------
# Parsing — segments, vias, and the net code→name table
# ---------------------------------------------------------------------------

def _parse_via(node: list[Any]) -> dict[str, Any] | None:
    """Parse one top-level ``(via …)`` node. Modeled on ``_parse_segment``.

    Shape: ``(via (at x y) (size s) (drill d) (layers "F.Cu" "B.Cu") (net N))``
    where ``N`` is a numeric code (KiCad ≤9) or a quoted name (KiCad 10).
    """
    via: dict[str, Any] = {}
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag == "at" and len(child) >= 3:
            via["x"] = float(child[1])
            via["y"] = float(child[2])
        elif tag == "size":
            via["size"] = float(child[1])
        elif tag == "drill":
            via["drill"] = float(child[1])
        elif tag == "layers" and len(child) >= 3:
            via["layers"] = [str(child[1]), str(child[2])]
        elif tag == "net":
            via["net"] = child[1]
    return via if "x" in via else None


def _arc_identity(node: list[Any]) -> tuple[Any, ...]:
    """Identity key for a top-level ``(arc …)`` track node.

    Arcs are NOT importable through the seam (the bridge payload has no arc
    shape) — this key only serves the fail-closed guard that detects arcs the
    routed copy added. Keyed on rounded start/mid/end + layer + width;
    endpoint order is normalized by sorting the tagged coordinate triples.
    """
    coords: list[tuple[str, float, float]] = []
    width = 0.0
    layer = ""
    for child in node[1:]:
        if not isinstance(child, list) or len(child) < 2:
            continue
        tag = child[0] if isinstance(child[0], str) else ""
        if tag in ("start", "mid", "end") and len(child) >= 3:
            coords.append((tag, _r(float(child[1])), _r(float(child[2]))))
        elif tag == "width":
            width = _r(float(child[1]))
        elif tag == "layer":
            layer = str(child[1])
    return (tuple(sorted(coords)), layer, width)


def _board_nodes(pcb_path: Path) -> list[Any]:
    """Top-level child nodes of a parsed ``.kicad_pcb`` file."""
    return parse_sexp_file(pcb_path)


def _net_code_table(tree: list[Any]) -> dict[int, str]:
    """``code → name`` from the file's top-level ``(net N "name")`` table.

    KiCad ≤9 files always carry it (at minimum ``(net 0 "")``); KiCad 10 files
    have no table at all — the returned dict is then empty and net references
    in segments/vias carry names directly.
    """
    table: dict[int, str] = {}
    for node in tree:
        if (
            isinstance(node, list)
            and len(node) >= 3
            and node[0] == "net"
            and isinstance(node[1], int)
        ):
            table[node[1]] = str(node[2])
    return table


def _resolve_net_name(raw: Any, code_to_name: dict[int, str], source: Path) -> str:
    """Net NAME for a parsed segment/via net reference.

    KiCad ≤9 references are numeric codes → mapped through the file's own net
    table; KiCad 10 references are names → passed through. Code 0 is always
    the unnamed net. An unmapped code means the routed file is internally
    inconsistent — refuse rather than import copper onto the wrong net.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    code = int(raw)
    if code == 0:
        return ""
    if code not in code_to_name:
        raise ValueError(
            f"net code {code} referenced in {source} has no entry in the "
            "file's top-level net table — cannot resolve it to a net name"
        )
    return code_to_name[code]


def _parse_routes(
    pcb_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[Any, ...]]]:
    """All (segments, vias, arc identity keys) of a board file.

    Segment/via nets are resolved to NAMES. Arcs are returned only as identity
    keys — the seam cannot import them, so they feed the fail-closed guard in
    :func:`extract_new_routes`.
    """
    tree = _board_nodes(pcb_path)
    code_to_name = _net_code_table(tree)
    segments: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    arcs: list[tuple[Any, ...]] = []
    for node in tree:
        if not isinstance(node, list) or not node:
            continue
        if node[0] == "segment":
            seg = _parse_segment_node(node)
            if seg is None:
                continue
            seg["net"] = _resolve_net_name(seg.get("net"), code_to_name, pcb_path)
            segments.append(seg)
        elif node[0] == "via":
            via = _parse_via(node)
            if via is None:
                continue
            if "size" not in via or "drill" not in via:
                # Never guess routed geometry: a via without an explicit size
                # or drill is a malformed/unexpected serialization, and
                # inventing defaults would silently manufacture copper.
                raise ValueError(
                    f"via at ({via['x']}, {via['y']}) in {pcb_path} is missing "
                    "its size or drill — refusing to guess routed geometry"
                )
            via["net"] = _resolve_net_name(via.get("net"), code_to_name, pcb_path)
            vias.append(via)
        elif node[0] == "arc":
            arcs.append(_arc_identity(node))
    return segments, vias, arcs


def _parse_segment_node(node: list[Any]) -> dict[str, Any] | None:
    """One ``(segment …)`` node in bridge-payload field names (flat floats)."""
    from kicad_mcp.backends.file_backend import _parse_segment

    track = _parse_segment(node)
    if track is None or "end" not in track:
        return None
    return {
        "start_x": track["start"]["x"],
        "start_y": track["start"]["y"],
        "end_x": track["end"]["x"],
        "end_y": track["end"]["y"],
        "width": track.get("width", 0.0),
        "layer": track.get("layer", ""),
        "net": track.get("net"),
    }


# ---------------------------------------------------------------------------
# Extraction — the added-routes diff
# ---------------------------------------------------------------------------

# NOTE: net is DELIBERATELY excluded from both identity keys below. Identity
# is the physical copper (geometry + layer + width); the net label is metadata
# a router may legitimately rewrite (e.g. code renumbering between files), and
# keying on it would make such items look "new" and re-import duplicates.
def _segment_key(seg: dict[str, Any]) -> tuple[Any, ...]:
    """Identity key: rounded endpoints (direction-normalized) + layer + width."""
    a = (_r(seg["start_x"]), _r(seg["start_y"]))
    b = (_r(seg["end_x"]), _r(seg["end_y"]))
    lo, hi = (a, b) if a <= b else (b, a)
    return (lo, hi, str(seg["layer"]), _r(seg["width"]))


def _via_key(via: dict[str, Any]) -> tuple[Any, ...]:
    """Identity key: rounded position + size + drill."""
    return (_r(via["x"]), _r(via["y"]), _r(via.get("size", 0.0)), _r(via.get("drill", 0.0)))


def extract_new_routes(input_pcb: Path, routed_pcb: Path) -> dict[str, Any]:
    """Segments/vias present in *routed_pcb* but absent from *input_pcb*.

    Returns ``{"segments": [...], "vias": [...]}`` in the ``add_routed_tracks``
    bridge payload shape: mm floats, layer name strings, net NAME strings
    (numeric net codes in KiCad ≤9 files are resolved through the file's own
    top-level net table; KiCad 10 files carry names directly).

    Raises ValueError when *routed_pcb* contains no routes at all — a routed
    output with zero copper is a router failure, never an "identical" result —
    and when the routed copy added ``(arc …)`` tracks, which the seam cannot
    carry (fail-closed: the bridge payload has no arc shape, so importing
    would silently drop copper that the file-replace fallback would keep).
    """
    routed_segments, routed_vias, routed_arcs = _parse_routes(routed_pcb)
    input_segments, input_vias, input_arcs = _parse_routes(input_pcb)

    new_arcs = Counter(routed_arcs) - Counter(input_arcs)
    if new_arcs:
        raise ValueError(
            "tracemaker output contains arc tracks, which the import seam "
            f"does not support: {routed_pcb} added {sum(new_arcs.values())} "
            "(arc …) node(s) not present in the routing input — refusing to "
            "import (the bridge payload would silently drop them)"
        )
    if not routed_segments and not routed_vias:
        raise ValueError(
            f"tracemaker output contains no routes: {routed_pcb} has zero "
            "segments and zero vias — refusing to treat it as an identical "
            "(nothing-new) result; the routing run likely failed"
        )

    # Multiset matching: N coincident identical items in the input consume
    # exactly N routed matches, so extra identical copies still count as new.
    remaining_segs = Counter(_segment_key(s) for s in input_segments)
    remaining_vias = Counter(_via_key(v) for v in input_vias)

    new_segments: list[dict[str, Any]] = []
    for seg in routed_segments:
        key = _segment_key(seg)
        if remaining_segs[key] > 0:
            remaining_segs[key] -= 1
            continue
        new_segments.append({
            "start_x": seg["start_x"],
            "start_y": seg["start_y"],
            "end_x": seg["end_x"],
            "end_y": seg["end_y"],
            "width": seg["width"],
            "layer": seg["layer"],
            "net": seg["net"],
        })

    new_vias: list[dict[str, Any]] = []
    for via in routed_vias:
        key = _via_key(via)
        if remaining_vias[key] > 0:
            remaining_vias[key] -= 1
            continue
        new_vias.append({
            "x": via["x"],
            "y": via["y"],
            "size": via["size"],
            "drill": via["drill"],
            "net": via["net"],
            "layers": via.get("layers", ["F.Cu", "B.Cu"]),
        })
    return {"segments": new_segments, "vias": new_vias}


# ---------------------------------------------------------------------------
# Import with file fallback
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_replace_fallback(
    board: Path,
    input_copy: Path,
    routed_pcb: Path,
    segments: list[dict[str, Any]],
    vias: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically replace *board* with *routed_pcb* — only if *board* is still
    byte-identical to *input_copy*, the exact file the router was given.

    If the board file changed since the routing copy was made, replacing it
    would silently discard those changes — refuse with no write instead.
    """
    board_hash = _sha256(board)
    input_hash = _sha256(input_copy)
    if board_hash != input_hash:
        raise RuntimeError(
            f"board file {board} changed since the routing copy was made "
            f"(sha256 {board_hash[:12]}… != routing-input {input_hash[:12]}…) "
            "— refusing to overwrite it with the routed copy; no write was "
            "performed. Re-run routing against the current board."
        )
    routed_bytes = routed_pcb.read_bytes()
    fd, tmp_name = tempfile.mkstemp(
        dir=str(board.parent), prefix=board.name + ".", suffix=".tm-import.tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(routed_bytes)
        os.replace(tmp_name, str(board))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {
        "success": True,
        "via": "file_replace",
        "new_tracks": len(segments),
        "new_vias": len(vias),
    }


def import_routed_tracks_with_fallback(
    backend: BackendProtocol, board: Path, input_copy: Path, routed_pcb: Path
) -> dict[str, Any]:
    """Apply TraceMaker's routed copy to the live board, with a safe fallback.

    Primary path: ``backend.add_routed_tracks(board, segments, vias)`` — the
    added-routes diff from :func:`extract_new_routes` goes to the live bridge
    board as one atomic batch.

    Fallback — the SAME three conditions as ``_import_ses_with_fallback``
    (stale bridge board, unreachable bridge, reachable-but-boardless bridge):
    no live board holds newer state than disk in any of these, so if the board
    file is still byte-identical to the copy the router was given, the routed
    file IS the board plus new copper — atomically replace the board file with
    it. If the hashes differ, refuse with no write.
    """
    from kicad_mcp.backends.plugin_backend import (
        BridgeTemporarilyUnavailableError,
        StaleBoardError,
    )
    from kicad_mcp.tools.routing import _is_boardless_bridge_error

    routes = extract_new_routes(input_copy, routed_pcb)
    segments: list[dict[str, Any]] = routes["segments"]
    vias: list[dict[str, Any]] = routes["vias"]

    try:
        result: dict[str, Any] = backend.add_routed_tracks(board, segments, vias)
        return result
    except StaleBoardError as exc:
        logger.warning(
            "add_routed_tracks: bridge board stale vs disk (%s); "
            "file-replace fallback (hash-gated)", exc
        )
        return _file_replace_fallback(board, input_copy, routed_pcb, segments, vias)
    except BridgeTemporarilyUnavailableError as exc:
        logger.warning(
            "add_routed_tracks: bridge unreachable (%s); "
            "file-replace fallback (hash-gated)", exc
        )
        return _file_replace_fallback(board, input_copy, routed_pcb, segments, vias)
    except Exception as exc:
        if _is_boardless_bridge_error(exc):
            logger.warning(
                "add_routed_tracks: bridge reachable but boardless (%s); "
                "file-replace fallback (hash-gated)", exc
            )
            return _file_replace_fallback(board, input_copy, routed_pcb, segments, vias)
        raise
