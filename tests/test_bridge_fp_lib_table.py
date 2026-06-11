"""Unit tests for the bridge's inline fp-lib-table parser (#1 bridge side).

The bridge cannot import the kicad_mcp package, so it carries a minimal
duplicate of the fp-lib-table parser. These tests pin the duplicate to the
same behavior as the canonical kicad_mcp.utils.fp_lib_table module. The
pcbnew-dependent resolution strategies are covered by the integration
suite (live pcbnew session).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BRIDGE_PATH = Path(__file__).parent.parent / "kicad_plugin" / "kicad_mcp_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("kicad_mcp_bridge_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE_TABLE = """\
(fp_lib_table
  (version 7)
  (lib (name Audio_Module)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty)(options "")(descr "Audio Module footprints"))
  (lib (name Seeed_XIAO)(type KiCad)(uri "C:/parts/Seeed_Studio_XIAO_Series.pretty")(options "")(descr "Seeed Studio XIAO Series"))
  (lib (name AirQuality_Project)(type KiCad)(uri "${KIPRJMOD}/AirQuality.pretty")(options "")(descr ""))
  (lib (name OldLib)(type KiCad)(uri "C:/old/OldLib.pretty")(options "")(descr "")(disabled))
)
"""


def test_parse_fp_lib_table_file(bridge, tmp_path: Path):
    table = tmp_path / "fp-lib-table"
    table.write_text(SAMPLE_TABLE, encoding="utf-8")

    entries = bridge._parse_fp_lib_table_file(str(table))

    assert entries["Audio_Module"] == "${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty"
    assert entries["Seeed_XIAO"] == "C:/parts/Seeed_Studio_XIAO_Series.pretty"
    assert entries["AirQuality_Project"] == "${KIPRJMOD}/AirQuality.pretty"
    assert "OldLib" not in entries  # disabled entries are skipped


def test_parse_fp_lib_table_file_missing(bridge, tmp_path: Path):
    assert bridge._parse_fp_lib_table_file(str(tmp_path / "nope")) == {}


def test_expand_lib_uri_kiprjmod(bridge):
    assert bridge._expand_lib_uri("${KIPRJMOD}/X.pretty", "D:/proj") == "D:/proj/X.pretty"
    assert bridge._expand_lib_uri("${KIPRJMOD}/X.pretty", None) is None


def test_expand_lib_uri_env(bridge, monkeypatch):
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", "D:/kicad/footprints")
    assert (
        bridge._expand_lib_uri("${KICAD9_FOOTPRINT_DIR}/R.pretty", None)
        == "D:/kicad/footprints/R.pretty"
    )


def test_expand_lib_uri_unknown_var(bridge, monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR_98765", raising=False)
    assert bridge._expand_lib_uri("${NO_SUCH_VAR_98765}/X.pretty", None) is None


def test_expand_lib_uri_absolute_passthrough(bridge):
    assert bridge._expand_lib_uri("C:/parts/X.pretty", None) == "C:/parts/X.pretty"
