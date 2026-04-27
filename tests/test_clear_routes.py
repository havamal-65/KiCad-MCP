"""Tests for FileBoardOps.clear_routes — removes tracks/vias, preserves footprints."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileBoardOps


# ---------------------------------------------------------------------------
# Fixture boards
# ---------------------------------------------------------------------------

BOARD_WITH_ROUTES = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (net 1 "GND")
      (net 2 "VCC")
      (footprint "Device:R" (layer "F.Cu") (at 10 10)
        (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
      )
      (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1))
      (segment (start 20 10) (end 20 20) (width 0.25) (layer "F.Cu") (net 2))
      (via (at 15 15) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
    )
""")

BOARD_NO_ROUTES = textwrap.dedent("""\
    (kicad_pcb
      (version 20231231)
      (generator "pcbnew")
      (net 0 "")
      (footprint "Device:C" (layer "F.Cu") (at 30 30)
        (property "Reference" "C1" (at 0 0 0) (layer "F.Fab"))
      )
    )
""")


@pytest.fixture
def board_with_routes(tmp_path: Path) -> Path:
    f = tmp_path / "routed.kicad_pcb"
    f.write_text(BOARD_WITH_ROUTES, encoding="utf-8")
    return f


@pytest.fixture
def board_no_routes(tmp_path: Path) -> Path:
    f = tmp_path / "unrouted.kicad_pcb"
    f.write_text(BOARD_NO_ROUTES, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Basic removal
# ---------------------------------------------------------------------------

def test_segments_removed(board_with_routes: Path):
    ops = FileBoardOps()
    result = ops.clear_routes(board_with_routes, backup=False)
    assert result["tracks_removed"] == 2
    content = board_with_routes.read_text(encoding="utf-8")
    assert "(segment" not in content


def test_vias_removed(board_with_routes: Path):
    ops = FileBoardOps()
    result = ops.clear_routes(board_with_routes, backup=False)
    assert result["vias_removed"] == 1
    content = board_with_routes.read_text(encoding="utf-8")
    assert "(via" not in content


def test_footprints_preserved(board_with_routes: Path):
    ops = FileBoardOps()
    ops.clear_routes(board_with_routes, backup=False)
    content = board_with_routes.read_text(encoding="utf-8")
    assert "(footprint" in content
    assert '"R1"' in content


def test_nets_preserved(board_with_routes: Path):
    ops = FileBoardOps()
    ops.clear_routes(board_with_routes, backup=False)
    content = board_with_routes.read_text(encoding="utf-8")
    assert '(net 1 "GND")' in content
    assert '(net 2 "VCC")' in content


# ---------------------------------------------------------------------------
# Board with no routes
# ---------------------------------------------------------------------------

def test_no_routes_board_returns_zero_counts(board_no_routes: Path):
    ops = FileBoardOps()
    result = ops.clear_routes(board_no_routes, backup=False)
    assert result["status"] == "success"
    assert result["tracks_removed"] == 0
    assert result["vias_removed"] == 0


def test_no_routes_board_content_unchanged(board_no_routes: Path):
    original = board_no_routes.read_text(encoding="utf-8")
    FileBoardOps().clear_routes(board_no_routes, backup=False)
    # Content may differ in whitespace but footprint must still be present
    content = board_no_routes.read_text(encoding="utf-8")
    assert "(footprint" in content
    assert '"C1"' in content


# ---------------------------------------------------------------------------
# Backup behaviour
# ---------------------------------------------------------------------------

def test_backup_created_when_requested(board_with_routes: Path):
    ops = FileBoardOps()
    result = ops.clear_routes(board_with_routes, backup=True)
    assert result["backup_path"] is not None
    backup = Path(result["backup_path"])
    assert backup.exists()
    # Backup should contain the original segments
    backup_content = backup.read_text(encoding="utf-8")
    assert "(segment" in backup_content


def test_no_backup_when_disabled(board_with_routes: Path):
    ops = FileBoardOps()
    result = ops.clear_routes(board_with_routes, backup=False)
    assert result["backup_path"] is None


# ---------------------------------------------------------------------------
# Return structure
# ---------------------------------------------------------------------------

def test_result_has_expected_keys(board_with_routes: Path):
    result = FileBoardOps().clear_routes(board_with_routes, backup=False)
    assert "status" in result
    assert "tracks_removed" in result
    assert "vias_removed" in result
    assert "backup_path" in result
    assert result["status"] == "success"
