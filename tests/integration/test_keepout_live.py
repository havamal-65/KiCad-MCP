"""REQ-KTEST-104 — keep-out gate on real geometry + the live batch (K1).

Two legs, mirroring ``test_placement_live.py``:

* ``test_real_esp32_board_gate_flags_antenna_intrusion`` runs on the
  real-KiCad-authored ESP32 fixture board whenever it is present — no bridge
  needed. It proves the gate against zone bytes *written by pcbnew itself*
  (the AC8 geometry, minus the live autoroute refusal).
* The ``bridge_session`` tests (opt-in, ``KICAD_INTEGRATION=1``) are the live
  batch: AC8 on the open board and the Q1 rotation confirmation
  (requirements §2 — embedded-zone points must rotate with the footprint when
  *pcbnew* performs the move).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps
from kicad_mcp.tools.drc import run_validate_placement_quality
from kicad_mcp.utils.keepout import scan_board

_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "boards"
    / "bt_audio_v1_before_connector_fix.kicad_pcb"
)


def _footprint_forbidding_keepouts(content: str):
    keepouts, _, _ = scan_board(content)
    return [k for k in keepouts if k.forbids_footprints]


def _zone_centroid(area) -> tuple[float, float]:
    pts = area.polygons[0]
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


@pytest.mark.integration
def test_real_esp32_board_gate_flags_antenna_intrusion(tmp_path: Path) -> None:
    """AC8 geometry on real-KiCad zone bytes: a part moved into the ESP32
    antenna keep-out fails the gate; moved out, no keep-out violation."""
    if not _FIXTURE.exists():
        pytest.skip("ESP32 fixture board not present")
    dst = tmp_path / _FIXTURE.name
    shutil.copy(_FIXTURE, dst)
    content = dst.read_text(encoding="utf-8")

    embedded = [
        k for k in _footprint_forbidding_keepouts(content)
        if k.origin.startswith("embedded:")
    ]
    if not embedded:
        pytest.skip("fixture carries no embedded footprint-forbidding keep-out")
    area = embedded[0]
    owner = area.origin.split(":", 1)[1]

    # Pick a small intruder: any real ref that is not the keep-out's owner.
    from kicad_mcp.tools.drc import _parse_placed_courtyards

    courtyards, _ = _parse_placed_courtyards(content)
    intruder = next(r for r in sorted(courtyards) if r != owner)

    cx, cy = _zone_centroid(area)
    ops = FileBoardOps()
    ops.move_component(dst, intruder, cx, cy)
    result = run_validate_placement_quality(dst)
    hits = [
        v for v in result["violations"]
        if v["type"] == "keepout_intrusion" and v["reference"] == intruder
    ]
    assert hits, result["violations"]
    assert result["passed"] is False
    assert hits[0]["keepout_origin"] == f"embedded:{owner}"

    # Move it far outside the zone bbox → no keep-out violation for it.
    xs = [p[0] for poly in area.polygons for p in poly]
    ops.move_component(dst, intruder, max(xs) + 30.0, cy)
    result = run_validate_placement_quality(dst)
    assert not [
        v for v in result["violations"]
        if v["type"] == "keepout_intrusion" and v["reference"] == intruder
    ], result["violations"]


@pytest.mark.integration
def test_kicad_drc_agrees_with_gate_on_file_placed_board(tmp_path: Path) -> None:
    """REQ-KWRITE-004 — KiCad itself understands our written zone coordinates.

    The file backend places the *real* stock ESP32-C3-WROOM-02 (embedded
    antenna keep-out transformed local→board by KWRITE) plus an intruding
    resistor inside that zone. Our gate flags it — and kicad-cli DRC, reading
    the same file, must report the keep-out violation too. If KWRITE wrote
    wrong coordinates, KiCad would see the zone at the origin and disagree.
    Runs whenever kicad-cli and the stock libraries are present (no bridge).
    """
    from kicad_mcp.backends.cli_backend import CLIDRCOps
    from kicad_mcp.backends.file_backend import _load_kicad_mod
    from kicad_mcp.utils.platform_helper import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")
    if _load_kicad_mod("RF_Module:ESP32-C3-WROOM-02") is None:
        pytest.skip("stock RF_Module library not available")

    p = tmp_path / "kwrite.kicad_pcb"
    p.write_text(
        '(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
        '\t(generator_version "9.0")\n\t(general (thickness 1.6))\n'
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)\n'
        '\t\t(31 "F.CrtYd" user "F.Courtyard") (36 "F.SilkS" user "F.Silkscreen")\n'
        '\t\t(35 "F.Fab" user) (37 "F.Paste" user) (39 "F.Mask" user)\n'
        '\t)\n\t(net 0 "")\n'
        '\t(gr_rect (start 0 0) (end 60 60) (stroke (width 0.1) (type solid)) '
        '(fill no) (layer "Edge.Cuts"))\n)\n',
        encoding="utf-8",
    )
    ops = FileBoardOps()
    ops.place_component(p, "U1", "RF_Module:ESP32-C3-WROOM-02", 30.0, 40.0)
    # Antenna keep-out now spans board y ≈ 21.9–32.9 — drop an intruder in it.
    ops.place_component(p, "R1", "Resistor_SMD:R_0805_2012Metric", 30.0, 27.0)

    gate = run_validate_placement_quality(p)
    assert any(
        v["type"] == "keepout_intrusion" and v["reference"] == "R1"
        for v in gate["violations"]
    ), gate["violations"]

    drc = CLIDRCOps(cli).run_drc(p)
    text = str(drc).lower()
    assert "not allowed" in text or "keepout" in text, (
        "kicad-cli DRC did not report the keep-out intrusion — "
        f"KWRITE coordinate disagreement? DRC: {drc}"
    )


@pytest.mark.integration
def test_live_gate_runs_on_open_board(bridge_session: object) -> None:
    """The gate executes against the board open in pcbnew and reports the
    extended-not-restructured shape (REQ-KGATE-005)."""
    from kicad_mcp.backends.plugin_backend import _tcp_call

    info = _tcp_call("ping", timeout=5.0)
    board_path = info.get("board_path") if isinstance(info, dict) else None
    if not board_path or not Path(board_path).exists():
        pytest.skip("no live board path available from the bridge")

    result = run_validate_placement_quality(Path(board_path))
    assert set(result) == {
        "passed", "placement_metric", "violations", "required_actions",
    }
    for v in result["violations"]:
        if v["type"] == "keepout_intrusion":
            assert v["severity"] == "blocking"
            assert result["passed"] is False


@pytest.mark.integration
def test_live_rotation_bakes_zone_points(bridge_session: object) -> None:
    """Q1 rotation confirmation (requirements §2): when *pcbnew* rotates a
    footprint with an embedded keep-out, the saved zone points move/rotate
    with it (board-absolute storage). The footprint is restored afterwards."""
    from kicad_mcp.backends.plugin_backend import PluginBoardOps, _tcp_call

    info = _tcp_call("ping", timeout=5.0)
    board_path = info.get("board_path") if isinstance(info, dict) else None
    if not board_path or not Path(board_path).exists():
        pytest.skip("no live board path available from the bridge")
    p = Path(board_path)

    content = p.read_text(encoding="utf-8")
    embedded = [
        k for k in _footprint_forbidding_keepouts(content)
        if k.origin.startswith("embedded:")
    ]
    if not embedded:
        pytest.skip("live board carries no embedded keep-out")
    ref = embedded[0].origin.split(":", 1)[1]
    before = set(embedded[0].polygons[0])

    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

    loc = find_footprint_block_by_reference(content, ref)
    assert loc is not None
    block = content[loc[0]:loc[1] + 1]
    fp_at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", block)
    assert fp_at is not None
    ox, oy = float(fp_at.group(1)), float(fp_at.group(2))
    orot = float(fp_at.group(3)) if fp_at.group(3) else 0.0

    ops = PluginBoardOps()
    try:
        ops.move_component(p, ref, ox, oy, rotation=orot + 90.0)
        ops.save_board(p)
        rotated_content = p.read_text(encoding="utf-8")
        rotated = [
            k for k in _footprint_forbidding_keepouts(rotated_content)
            if k.origin == f"embedded:{ref}"
        ]
        assert rotated, "keep-out lost after live rotation"
        after = set(rotated[0].polygons[0])
        # Board-absolute storage: pcbnew rewrote the points — a pure header
        # rotation with untouched points would leave them identical.
        assert after != before, (
            "zone points did not change under live rotation — "
            "board-absolute assumption violated"
        )
    finally:
        ops.move_component(p, ref, ox, oy, rotation=orot)
        ops.save_board(p)
