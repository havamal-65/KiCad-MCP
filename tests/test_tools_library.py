"""Tests for library tools: project_dir threading (#10) and list_libraries
summary/filter/pagination (#11)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fastmcp
import pytest

from kicad_mcp.tools import library
from kicad_mcp.utils.change_log import ChangeLog

FIXTURE_MOD = """\
(footprint "R_Test"
  (layer "F.Cu")
  (fp_rect (start -1.5 -1) (end 1.5 1) (layer "F.CrtYd") (width 0.05))
  (pad "1" smd rect (at -0.9 0) (size 1 1) (layers "F.Cu"))
  (pad "2" smd rect (at 0.9 0) (size 1 1) (layers "F.Cu"))
)
"""


def _get_tools(backend_stub, tmp_path: Path) -> dict:
    mcp = fastmcp.FastMCP("test")
    change_log = ChangeLog(tmp_path / "changes.json")
    library.register_tools(mcp, backend_stub, change_log)
    return {t.name: t.fn for t in mcp._tool_manager._tools.values()}


def _make_project_with_lib(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    lib = project / "AirQuality.pretty"
    lib.mkdir(parents=True)
    (lib / "R_Test.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    (project / "fp-lib-table").write_text(
        "(fp_lib_table\n"
        '  (lib (name AirQuality_Project)(type KiCad)(uri "${KIPRJMOD}/AirQuality.pretty")(options "")(descr ""))\n'
        ")",
        encoding="utf-8",
    )
    return project


@pytest.fixture()
def _no_system_libs():
    with patch(
        "kicad_mcp.utils.kicad_paths.get_system_library_paths", return_value=[]
    ), patch(
        "kicad_mcp.utils.fp_lib_table.get_global_fp_lib_table_path", return_value=None
    ), patch(
        "kicad_mcp.utils.kicad_paths.find_symbol_libraries", return_value=[]
    ):
        yield


# ---------------------------------------------------------------------------
# #10 — get_footprint_bounds / search_footprints honor project_dir
# ---------------------------------------------------------------------------

def test_get_footprint_bounds_with_project_dir(tmp_path: Path, _no_system_libs):
    project = _make_project_with_lib(tmp_path)
    tools = _get_tools(MagicMock(), tmp_path)

    result = json.loads(
        tools["get_footprint_bounds"]("AirQuality_Project:R_Test", project_dir=str(project))
    )
    assert result["status"] == "success"
    assert result["width_mm"] == 3.0
    assert result["height_mm"] == 2.0


def test_get_footprint_bounds_without_project_dir_not_found(tmp_path: Path, _no_system_libs):
    tools = _get_tools(MagicMock(), tmp_path)

    result = json.loads(tools["get_footprint_bounds"]("AirQuality_Project:R_Test"))
    assert result["status"] == "error"
    assert "project_dir" in result["message"]


def test_search_footprints_with_project_dir(tmp_path: Path, _no_system_libs):
    project = _make_project_with_lib(tmp_path)
    tools = _get_tools(MagicMock(), tmp_path)

    result = json.loads(tools["search_footprints"]("R_Test", project_dir=str(project)))
    assert result["count"] == 1
    assert result["footprints"][0]["lib_id"] == "AirQuality_Project:R_Test"


def test_search_footprints_without_project_dir_uses_backend(tmp_path: Path):
    backend_stub = MagicMock()
    backend_stub.get_library_ops.return_value.search_footprints.return_value = []
    tools = _get_tools(backend_stub, tmp_path)

    result = json.loads(tools["search_footprints"]("R_Test"))
    assert result["count"] == 0
    backend_stub.get_library_ops.assert_called_once()


# ---------------------------------------------------------------------------
# #11 — list_libraries summary mode, filters, pagination
# ---------------------------------------------------------------------------

def _fake_backend_with_libs(tmp_path: Path) -> MagicMock:
    """Backend stub: 3 symbol libs + 2 footprint libs (real dirs for counts)."""
    fp_a = tmp_path / "Connector.pretty"
    fp_a.mkdir()
    (fp_a / "PinHeader_1x04.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    (fp_a / "PinHeader_1x02.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")
    fp_b = tmp_path / "Resistor_SMD.pretty"
    fp_b.mkdir()
    (fp_b / "R_0805.kicad_mod").write_text(FIXTURE_MOD, encoding="utf-8")

    libs = [
        {"name": "Device", "type": "symbol", "path": str(tmp_path / "Device.kicad_sym")},
        {"name": "Connector", "type": "symbol", "path": str(tmp_path / "Connector.kicad_sym")},
        {"name": "MCU_Module", "type": "symbol", "path": str(tmp_path / "MCU_Module.kicad_sym")},
        {"name": "Connector", "type": "footprint", "path": str(fp_a)},
        {"name": "Resistor_SMD", "type": "footprint", "path": str(fp_b)},
    ]
    backend_stub = MagicMock()
    backend_stub.get_library_ops.return_value.list_libraries.return_value = libs
    return backend_stub


def test_list_libraries_summary_default(tmp_path: Path):
    tools = _get_tools(_fake_backend_with_libs(tmp_path), tmp_path)

    result = json.loads(tools["list_libraries"]())
    assert result["total_matched"] == 5
    assert result["symbol_libraries"] == 3
    assert result["footprint_libraries"] == 2
    # Summary entries carry no paths but footprint entries carry counts.
    for entry in result["libraries"]:
        assert "path" not in entry
    fp_connector = next(
        e for e in result["libraries"]
        if e["name"] == "Connector" and e["type"] == "footprint"
    )
    assert fp_connector["entries"] == 2


def test_list_libraries_detail_mode_has_paths(tmp_path: Path):
    tools = _get_tools(_fake_backend_with_libs(tmp_path), tmp_path)

    result = json.loads(tools["list_libraries"](summary=False))
    assert all("path" in e for e in result["libraries"])


def test_list_libraries_kind_filter(tmp_path: Path):
    tools = _get_tools(_fake_backend_with_libs(tmp_path), tmp_path)

    result = json.loads(tools["list_libraries"](kind="footprints"))
    assert result["total_matched"] == 2
    assert all(e["type"] == "footprint" for e in result["libraries"])


def test_list_libraries_name_filter(tmp_path: Path):
    tools = _get_tools(_fake_backend_with_libs(tmp_path), tmp_path)

    result = json.loads(tools["list_libraries"](name_filter="connector"))
    assert result["total_matched"] == 2
    assert {e["type"] for e in result["libraries"]} == {"symbol", "footprint"}


def test_list_libraries_pagination(tmp_path: Path):
    tools = _get_tools(_fake_backend_with_libs(tmp_path), tmp_path)

    page1 = json.loads(tools["list_libraries"](limit=2))
    assert page1["returned"] == 2
    assert page1["total_matched"] == 5

    page2 = json.loads(tools["list_libraries"](limit=2, offset=2))
    assert page2["returned"] == 2
    assert page2["offset"] == 2
    assert [e["name"] for e in page1["libraries"]] != [e["name"] for e in page2["libraries"]]

    page3 = json.loads(tools["list_libraries"](limit=2, offset=4))
    assert page3["returned"] == 1


def test_list_libraries_project_dir_includes_project_libs(tmp_path: Path, _no_system_libs):
    project = _make_project_with_lib(tmp_path)
    tools = _get_tools(MagicMock(), tmp_path)

    result = json.loads(tools["list_libraries"](project_dir=str(project)))
    names = [e["name"] for e in result["libraries"]]
    assert "AirQuality_Project" in names
