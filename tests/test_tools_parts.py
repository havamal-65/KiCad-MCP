"""Unit tests for the parts catalog MCP tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.tools import parts as parts_module
from kicad_mcp.utils import library_sources as lib_sources_module
from kicad_mcp.utils import parts_index as parts_index_module
from kicad_mcp.utils.change_log import ChangeLog


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the default config dir at ``~/.kicad-mcp`` to ``tmp_path``.

    The parts module instantiates ``PartsIndex()`` and
    ``LibrarySourceRegistry()`` with their default paths inside
    ``register_tools``; we have to override those defaults before
    registering or the tests will scribble on the developer's home dir.
    """
    monkeypatch.setattr(parts_index_module, "DEFAULT_INDEX_PATH",
                        tmp_path / "parts_index.sqlite")
    monkeypatch.setattr(lib_sources_module, "DEFAULT_REGISTRY_FILE",
                        tmp_path / "library_sources.json")
    return tmp_path


@pytest.fixture
def mcp_app(
    mock_composite: BackendProtocol,
    tmp_change_log: ChangeLog,
    isolated_paths: Path,
) -> FastMCP:
    mcp = FastMCP("test-parts")
    parts_module.register_tools(mcp, mock_composite, tmp_change_log)
    return mcp


@pytest.fixture
def tools(mcp_app: FastMCP) -> dict:
    return {t.name: t.fn for t in mcp_app._tool_manager._tools.values()}


class TestListKnownSources:
    def test_returns_catalog(self, tools):
        result = json.loads(tools["list_known_sources"]())
        assert result["status"] == "success"
        names = {s["name"] for s in result["sources"]}
        assert {"digikey", "sparkfun", "snapmagic", "octopart"} <= names


class TestBootstrapKnownSource:
    def test_unknown_source_errors(self, tools):
        result = json.loads(tools["bootstrap_known_source"](name="nope"))
        assert result["status"] == "error"

    def test_git_source_calls_clone(self, tools):
        # The mock backend's clone_library_repo just echoes back; we just
        # need the tool to dispatch through it without raising.
        result = json.loads(tools["bootstrap_known_source"](name="digikey"))
        assert result["status"] == "success"
        assert result["kind"] == "git"
        assert "next_step" in result

    def test_api_source_reports_auth_state(self, tools, monkeypatch):
        # Without the env var, status should be needs_auth.
        monkeypatch.delenv("SNAPMAGIC_API_KEY", raising=False)
        result = json.loads(tools["bootstrap_known_source"](name="snapmagic"))
        assert result["status"] == "needs_auth"
        assert result["auth_present"] is False

        monkeypatch.setenv("SNAPMAGIC_API_KEY", "test-token")
        result = json.loads(tools["bootstrap_known_source"](name="snapmagic"))
        assert result["status"] == "success"
        assert result["auth_present"] is True

    def test_web_source_returns_homepage(self, tools):
        result = json.loads(tools["bootstrap_known_source"](name="pcb-libraries-pro"))
        assert result["status"] == "manual"
        assert result["kind"] == "web"
        assert result["homepage"]


class TestIndexLibrarySource:
    def test_indexes_local_fixture(self, tools, isolated_paths: Path):
        # Register the fixture symbols directory through the tool layer.
        from kicad_mcp.utils.library_sources import LibrarySourceRegistry
        reg = LibrarySourceRegistry(registry_path=isolated_paths / "library_sources.json")
        reg.register("symbols_fix", str(FIXTURES / "symbols"), source_type="local")

        result = json.loads(tools["index_library_source"](source_name="symbols_fix"))
        assert result["status"] == "success"
        assert result["indexed"] > 0
        assert result["stats"]["per_source"]["symbols_fix"] > 0

    def test_index_all_runs_every_registered_source(self, tools, isolated_paths: Path):
        from kicad_mcp.utils.library_sources import LibrarySourceRegistry
        reg = LibrarySourceRegistry(registry_path=isolated_paths / "library_sources.json")
        reg.register("syms", str(FIXTURES / "symbols"), source_type="local")
        reg.register("fps", str(FIXTURES / "footprints"), source_type="local")

        result = json.loads(tools["index_library_source"](source_name="all"))
        assert result["status"] == "success"
        assert result["indexed_sources"] == 2


class TestSearchParts:
    def test_search_after_index_returns_hits(self, tools, isolated_paths: Path):
        from kicad_mcp.utils.library_sources import LibrarySourceRegistry
        reg = LibrarySourceRegistry(registry_path=isolated_paths / "library_sources.json")
        reg.register("syms", str(FIXTURES / "symbols"), source_type="local")
        json.loads(tools["index_library_source"](source_name="syms"))

        result = json.loads(tools["search_parts"](query="Device", limit=10))
        assert result["status"] == "success"
        assert result["count"] >= 1

    def test_search_filters_by_package(self, tools, isolated_paths: Path):
        from kicad_mcp.utils.library_sources import LibrarySourceRegistry
        reg = LibrarySourceRegistry(registry_path=isolated_paths / "library_sources.json")
        reg.register("fps", str(FIXTURES / "footprints"), source_type="local")
        json.loads(tools["index_library_source"](source_name="fps"))

        result = json.loads(tools["search_parts"](package="0805"))
        assert result["status"] == "success"
        assert all(p.get("package") == "0805" for p in result["parts"])

    def test_empty_index_returns_zero(self, tools):
        result = json.loads(tools["search_parts"](query="anything"))
        assert result["status"] == "success"
        assert result["count"] == 0


class TestInstallPart:
    def test_unknown_source_errors(self, tools):
        # ingester_for_source returns LocalLibsIngester for unknown names,
        # which doesn't support fetch_part — should surface a clear message.
        result = json.loads(tools["install_part"](mpn="X", source="unknown"))
        # Either an error status or info status is acceptable; the key is
        # that we don't blow up and we surface a useful message.
        assert "message" in result or "error" in result

    def test_api_source_without_token_returns_info(self, tools, monkeypatch):
        monkeypatch.delenv("SNAPMAGIC_API_KEY", raising=False)
        result = json.loads(tools["install_part"](mpn="STM32F407VGT6", source="snapmagic"))
        assert result["status"] == "info"
        assert "SNAPMAGIC_API_KEY" in result["message"]


class TestPartsIndexStats:
    def test_empty_index(self, tools):
        result = json.loads(tools["parts_index_stats"]())
        assert result["status"] == "success"
        assert result["total"] == 0
