"""Unit tests for the static known-sources catalog."""

from __future__ import annotations

from kicad_mcp.utils.known_sources import (
    get_known_source,
    known_source_names,
    list_known_sources,
)


class TestKnownSourcesCatalog:
    def test_includes_expected_sources(self):
        names = set(known_source_names())
        # The third-party sources called out in the project plan.
        for required in ("digikey", "sparkfun", "wurth-elektronik",
                         "snapmagic", "ultra-librarian", "octopart",
                         "pcb-libraries-pro"):
            assert required in names, f"missing {required} from catalog"

    def test_lookup_is_case_insensitive(self):
        s = get_known_source("DIGIKEY")
        assert s is not None
        assert s.name == "digikey"

    def test_unknown_source_returns_none(self):
        assert get_known_source("nope-not-real") is None

    def test_api_sources_declare_auth_env(self):
        for name in ("snapmagic", "ultra-librarian", "octopart"):
            s = get_known_source(name)
            assert s is not None
            assert s.kind == "api"
            assert s.requires_auth is True
            assert s.auth_env_var, f"{name} should declare an auth env var"

    def test_git_sources_have_clone_url(self):
        for name in ("digikey", "sparkfun", "wurth-elektronik",
                     "kicad-symbols", "kicad-footprints"):
            s = get_known_source(name)
            assert s is not None
            assert s.kind == "git"
            assert s.url.endswith(".git") or "git" in s.url

    def test_list_returns_dicts(self):
        items = list_known_sources()
        assert isinstance(items, list)
        assert all(isinstance(item, dict) for item in items)
        assert all("name" in item and "kind" in item for item in items)
