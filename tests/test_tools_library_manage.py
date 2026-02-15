"""Unit tests for library management tools using mocked backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp import FastMCP

from kicad_mcp.backends.composite import CompositeBackend
from kicad_mcp.tools import library_manage
from kicad_mcp.utils.change_log import ChangeLog


@pytest.fixture
def mcp_app(mock_composite: CompositeBackend, tmp_change_log: ChangeLog) -> FastMCP:
    mcp = FastMCP("test")
    library_manage.register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


@pytest.fixture
def tools(mcp_app: FastMCP) -> dict:
    """Return a mapping of tool name -> callable."""
    return {t.name: t.fn for t in mcp_app._tool_manager._tools.values()}


class TestCloneLibraryRepo:
    def test_success(self, tools):
        result = json.loads(tools["clone_library_repo"](
            url="https://github.com/example/kicad-lib.git",
            name="example",
        ))
        assert result["status"] == "success"
        assert result["name"] == "example"
        assert result["source_type"] == "git"

    def test_with_target_path(self, tools):
        result = json.loads(tools["clone_library_repo"](
            url="https://github.com/example/kicad-lib.git",
            name="example",
            target_path="/tmp/my_libs",
        ))
        assert result["status"] == "success"
        assert result["path"] == "/tmp/my_libs"


class TestRegisterLibrarySource:
    def test_success(self, tools):
        result = json.loads(tools["register_library_source"](
            path="/some/local/path",
            name="my_libs",
        ))
        assert result["status"] == "success"
        assert result["name"] == "my_libs"
        assert result["source_type"] == "local"


class TestListLibrarySources:
    def test_success(self, tools):
        result = json.loads(tools["list_library_sources"]())
        assert result["status"] == "success"
        assert result["count"] >= 1
        assert isinstance(result["sources"], list)


class TestUnregisterLibrarySource:
    def test_success(self, tools):
        result = json.loads(tools["unregister_library_source"](name="mock_source"))
        assert result["status"] == "success"
        assert result["removed"] is True


class TestSearchLibrarySources:
    def test_success(self, tools):
        result = json.loads(tools["search_library_sources"](query="Mock"))
        assert result["status"] == "success"
        assert result["symbol_count"] >= 1

    def test_with_source_filter(self, tools):
        result = json.loads(tools["search_library_sources"](
            query="Mock", source_name="mock_source",
        ))
        assert result["status"] == "success"


class TestCreateProjectLibrary:
    def test_success(self, tools):
        result = json.loads(tools["create_project_library"](
            project_path="/some/project.kicad_pro",
            library_name="my_project_lib",
        ))
        assert result["status"] == "success"
        assert result["library_name"] == "my_project_lib"

    def test_symbol_only(self, tools):
        result = json.loads(tools["create_project_library"](
            project_path="/some/project.kicad_pro",
            library_name="my_symbols",
            lib_type="symbol",
        ))
        assert result["status"] == "success"


class TestImportSymbol:
    def test_success(self, tools):
        result = json.loads(tools["import_symbol"](
            source_lib="/libs/source.kicad_sym",
            symbol_name="SCD41",
            target_lib_path="/project/lib.kicad_sym",
        ))
        assert result["status"] == "success"
        assert result["symbol_name"] == "SCD41"


class TestImportFootprint:
    def test_success(self, tools):
        result = json.loads(tools["import_footprint"](
            source_lib="/libs/source.pretty",
            footprint_name="SCD41",
            target_lib_path="/project/lib.pretty",
        ))
        assert result["status"] == "success"
        assert result["footprint_name"] == "SCD41"


class TestRegisterProjectLibrary:
    def test_success(self, tools):
        result = json.loads(tools["register_project_library"](
            project_path="/some/project.kicad_pro",
            library_name="my_lib",
            library_path="/some/my_lib.kicad_sym",
            lib_type="symbol",
        ))
        assert result["status"] == "success"
        assert result["library_name"] == "my_lib"
        assert result["lib_type"] == "symbol"
