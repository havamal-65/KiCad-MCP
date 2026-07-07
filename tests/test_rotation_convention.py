"""Regression tests for the KiCad footprint-rotation convention (bug found 2026-07-03).

Ground truth, taken from a real pcbnew-written board (aqs_v2, USB4085 receptacle
placed by the bridge at rotation 90) and confirmed by KiCad's own DRC engine:

    footprint (at 11.11 22.025 90), pad A12 stored footprint-local (at 5.95 0 90)
    KiCad DRC reports pad A12 at board position (11.11, 16.075)

Therefore KiCad maps a footprint-local point (x, y) at rotation theta to:

    x_board = X + x*cos(theta) + y*sin(theta)
    y_board = Y - x*sin(theta) + y*cos(theta)

(positive theta = counterclockwise on screen = clockwise in file coordinates,
because the file Y axis points down). The transposed form
``x*cos - y*sin / x*sin + y*cos`` agrees at 0/180 degrees and for symmetric
shapes, which is how it survived earlier live verification, but is wrong at
90/270 — on the originating board it hid a real J1/J3 courtyard overlap and
let both edge connectors face into the board interior.

Every local->board transform in the board domain must follow the convention
above; these tests pin each site to the pcbnew-observed values.
"""

from __future__ import annotations

import json
import math

from kicad_mcp.utils import placement_engine as engine
from kicad_mcp.utils.keepout import transform_polygon, untransform_polygon
from kicad_mcp.utils.placement_metrics import read_board_pads
from kicad_mcp.tools.drc import (
    _parse_placed_courtyards,
    _rotate_vec,
    _suggested_rotation_for_edge,
    run_check_courtyard_overlaps,
)
from kicad_mcp.backends.file_backend import FileBoardOps, _zone_local_to_board


# Ground-truth constants from the aqs_v2 board (see module docstring).
_J1_ORIGIN = (11.11, 22.025)
_J1_ROT = 90.0
_A12_LOCAL = (5.95, 0.0)
_A12_BOARD = (11.11, 16.075)

# USB4085 courtyard, footprint-local frame (from get_footprint_bounds).
_J1_CYD_LOCAL = (-2.3, -1.06, 8.25, 9.11)
# Expected board-frame AABB at rotation 90 under the KiCad convention:
#   x spans X + [ymin, ymax] = [10.05, 20.22]  (wait: x = X + x*c + y*s = X + y)
#   y spans Y - [xmax, xmin] = [13.775, 24.325]
_J1_CYD_BOARD = (10.05, 13.775, 20.22, 24.325)


def _fp_block(
    ref: str,
    x: float,
    y: float,
    rot: float,
    pads: str = "",
    courtyard: tuple[float, float, float, float] | None = None,
    zones: str = "",
) -> str:
    rot_s = f" {rot:g}" if rot else ""
    cyd = ""
    if courtyard is not None:
        cx0, cy0, cx1, cy1 = courtyard
        cyd = (
            f'\t\t(fp_rect\n\t\t\t(start {cx0} {cy0})\n\t\t\t(end {cx1} {cy1})\n'
            f'\t\t\t(stroke (width 0.05) (type default))\n'
            f'\t\t\t(fill none)\n\t\t\t(layer "F.CrtYd")\n\t\t)\n'
        )
    return (
        f'\t(footprint "Test:{ref}"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(at {x} {y}{rot_s})\n'
        f'\t\t(property "Reference" "{ref}"\n'
        f'\t\t\t(at 0 -3 0)\n\t\t\t(layer "F.SilkS")\n'
        f'\t\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t\t)\n'
        f'{cyd}{pads}{zones}'
        f'\t)\n'
    )


def _board(*footprints: str) -> str:
    body = "".join(footprints)
    return (
        '(kicad_pcb\n'
        '\t(version 20241229)\n'
        '\t(generator "test")\n'
        '\t(general (thickness 1.6))\n'
        '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal)\n'
        '\t\t(36 "F.CrtYd" user) (38 "F.SilkS" user) (44 "Edge.Cuts" user))\n'
        '\t(net 0 "")\n'
        '\t(net 1 "GND")\n'
        f'{body}'
        '\t(gr_rect (start 0 0) (end 70 50)\n'
        '\t\t(stroke (width 0.05) (type default)) (fill none)\n'
        '\t\t(layer "Edge.Cuts"))\n'
        ')\n'
    )


_PAD_A12 = (
    '\t\t(pad "A12" thru_hole circle\n'
    '\t\t\t(at 5.95 0 90)\n'
    '\t\t\t(size 0.7 0.7)\n'
    '\t\t\t(drill 0.4)\n'
    '\t\t\t(layers "*.Cu" "*.Mask")\n'
    '\t\t\t(net 1 "GND")\n'
    '\t\t)\n'
)


class TestPadPositions:
    def test_metrics_pad_matches_pcbnew_at_rot_90(self) -> None:
        content = _board(_fp_block("J1", *_J1_ORIGIN, _J1_ROT, pads=_PAD_A12))
        pads = read_board_pads(content)
        assert len(pads) == 1
        assert math.isclose(pads[0]["x_mm"], _A12_BOARD[0], abs_tol=1e-6)
        assert math.isclose(pads[0]["y_mm"], _A12_BOARD[1], abs_tol=1e-6)

    def test_metrics_pad_rot_270(self) -> None:
        # (5.95, 0) at 270: x = X - 0 = X ... x = X + x*cos270 + y*sin270 = X + 0
        # -> x = X; y = Y - x*sin270 = Y + 5.95
        content = _board(_fp_block("J1", *_J1_ORIGIN, 270.0, pads=_PAD_A12))
        pads = read_board_pads(content)
        assert math.isclose(pads[0]["x_mm"], 11.11, abs_tol=1e-6)
        assert math.isclose(pads[0]["y_mm"], 22.025 + 5.95, abs_tol=1e-6)


class TestCourtyards:
    def test_drc_courtyard_aabb_at_rot_90(self) -> None:
        content = _board(
            _fp_block("J1", *_J1_ORIGIN, _J1_ROT, courtyard=_J1_CYD_LOCAL)
        )
        courtyards, missing = _parse_placed_courtyards(content)
        assert not missing
        box = courtyards["J1"]
        assert math.isclose(box["xmin"], _J1_CYD_BOARD[0], abs_tol=1e-6)
        assert math.isclose(box["ymin"], _J1_CYD_BOARD[1], abs_tol=1e-6)
        assert math.isclose(box["xmax"], _J1_CYD_BOARD[2], abs_tol=1e-6)
        assert math.isclose(box["ymax"], _J1_CYD_BOARD[3], abs_tol=1e-6)

    def test_overlap_checker_catches_live_j1_j3_miss(self, tmp_path) -> None:
        """The exact overlap KiCad DRC flagged on aqs_v2 and the file check missed."""
        content = _board(
            _fp_block("J1", *_J1_ORIGIN, _J1_ROT, courtyard=_J1_CYD_LOCAL),
            _fp_block(
                "J3", 10.5912, 9.9, 0.0,
                courtyard=(-1.77, -1.77, 1.77, 9.39),
            ),
        )
        p = tmp_path / "t.kicad_pcb"
        p.write_text(content, encoding="utf-8")
        result = run_check_courtyard_overlaps(p)
        assert result["passed"] is False
        pairs = {
            frozenset((o["ref_a"], o["ref_b"])) for o in result["overlaps"]
        }
        assert frozenset(("J1", "J3")) in pairs

    def test_engine_board_box_at_rot_90(self) -> None:
        part: engine.PartRecord = {
            "ref": "J1",
            "lib_id": "Test:J1",
            "cluster_key": "",
            "pad_count": 0,
            "courtyard": _J1_CYD_LOCAL,
            "pads": [],
            "pos": (0.0, 0.0, 0.0),
        }
        box = engine._board_box(part, _J1_ORIGIN, _J1_ROT)
        for got, want in zip(box, _J1_CYD_BOARD):
            assert math.isclose(got, want, abs_tol=1e-6)


class TestVectorsAndEdges:
    def test_rotate_vec_kicad_convention(self) -> None:
        # +y local face at footprint rotation 90 points +x on the board
        # (observed on aqs_v2: USB opening ended up facing east).
        assert _close_vec(_rotate_vec(0.0, 1.0, 90.0), (1.0, 0.0))
        assert _close_vec(_rotate_vec(0.0, 1.0, 270.0), (-1.0, 0.0))
        assert _close_vec(_rotate_vec(1.0, 0.0, 90.0), (0.0, -1.0))
        assert _close_vec(_rotate_vec(1.0, 0.0, 180.0), (-1.0, 0.0))

    def test_suggested_rotation_places_face_outward(self) -> None:
        # Solve then verify through _rotate_vec — self-consistency plus
        # the known-good pairs.
        from kicad_mcp.tools.drc import _FACE_VECTORS, _EDGE_OUTWARD_FACE

        for local_face in ("+x", "-x", "+y", "-y"):
            for edge in ("north", "south", "east", "west"):
                rot = _suggested_rotation_for_edge(local_face, edge)
                lv = _FACE_VECTORS[local_face]
                bv = _rotate_vec(lv[0], lv[1], rot)
                ov = _FACE_VECTORS[_EDGE_OUTWARD_FACE[edge]]
                assert _close_vec(bv, ov), (
                    f"{local_face} @ {edge}: rot {rot} gives {bv}, want {ov}"
                )
        # The aqs_v2 case: +y mating face anchored at the west edge needs 270.
        assert _suggested_rotation_for_edge("+y", "west") == 270.0


class TestKeepoutTransforms:
    def test_transform_polygon_matches_pcbnew(self) -> None:
        poly = ((_A12_LOCAL),)
        out = transform_polygon(poly, _J1_ORIGIN[0], _J1_ORIGIN[1], _J1_ROT)
        assert _close_vec(out[0], _A12_BOARD)

    def test_round_trip_all_rotations(self) -> None:
        poly = ((1.0, 2.0), (-3.5, 0.25), (4.125, -7.75))
        for rot in (0.0, 37.5, 90.0, 180.0, 270.0):
            board = transform_polygon(poly, 12.3, -4.5, rot)
            back = untransform_polygon(board, 12.3, -4.5, rot)
            for got, want in zip(back, poly):
                assert _close_vec(got, want)


class TestZoneWrites:
    def test_zone_local_to_board_matches_pcbnew(self) -> None:
        got = _zone_local_to_board(
            *_A12_LOCAL, _J1_ORIGIN[0], _J1_ORIGIN[1], _J1_ROT
        )
        assert _close_vec(got, _A12_BOARD)

    def test_move_component_rotates_embedded_zone(self, tmp_path) -> None:
        """Rotating a footprint 0 -> 90 must move a board-absolute zone point
        the way pcbnew would: local (1, 0) at rot 90 lands at (X, Y-1)."""
        zone = (
            '\t\t(zone\n'
            '\t\t\t(net 0)\n\t\t\t(net_name "")\n'
            '\t\t\t(layers "F.Cu")\n'
            '\t\t\t(hatch edge 0.5)\n'
            '\t\t\t(keepout (tracks not_allowed) (vias not_allowed)'
            ' (pads not_allowed) (copperpour not_allowed)'
            ' (footprints not_allowed))\n'
            '\t\t\t(polygon\n\t\t\t\t(pts\n'
            '\t\t\t\t\t(xy 11 10)\n'
            '\t\t\t\t\t(xy 12 10)\n'
            '\t\t\t\t\t(xy 12 11)\n'
            '\t\t\t\t)\n\t\t\t)\n'
            '\t\t)\n'
        )
        content = _board(_fp_block("U9", 10.0, 10.0, 0.0, zones=zone))
        p = tmp_path / "t.kicad_pcb"
        p.write_text(content, encoding="utf-8")

        ops = FileBoardOps()
        ops.move_component(p, "U9", 10.0, 10.0, rotation=90.0)

        moved = p.read_text(encoding="utf-8")
        # local points were (1,0), (2,0), (2,1); at rot 90:
        # (x,y) -> (X + y, Y - x)
        assert "(xy 10 9)" in moved
        assert "(xy 10 8)" in moved
        assert "(xy 11 8)" in moved


def _close_vec(
    got: tuple[float, float], want: tuple[float, float], tol: float = 1e-6
) -> bool:
    return math.isclose(got[0], want[0], abs_tol=tol) and math.isclose(
        got[1], want[1], abs_tol=tol
    )
