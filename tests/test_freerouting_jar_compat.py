"""FreeRouting JAR / Java compatibility guard (#22 resolution).

find_freerouting_jar must never return a JAR that needs a newer Java than the runtime,
so an UnsupportedClassVersionError can't happen at route time (v2.2.4 needs Java 25,
v2.1.0 needs 21, v1.9.0 needs 17).
"""
from kicad_mcp.utils.platform_helper import _freerouting_min_java, find_freerouting_jar


def test_min_java_by_lineage():
    assert _freerouting_min_java("freerouting-2.2.4.jar") == 25
    assert _freerouting_min_java("freerouting-2.1.0.jar") == 21
    assert _freerouting_min_java("freerouting-1.9.0.jar") == 17
    assert _freerouting_min_java("freerouting-2.2.0.jar") == 25
    assert _freerouting_min_java("not-a-freerouting.jar") == 17  # unknown -> safe floor


def _make_jars(tmp_path, monkeypatch, names):
    frdir = tmp_path / ".kicad-mcp" / "freerouting"
    frdir.mkdir(parents=True)
    for n in names:
        (frdir / n).write_bytes(b"PK\x03\x04")  # any bytes; selection is by filename
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return frdir


def test_java21_skips_v2_2_and_picks_v1_9(tmp_path, monkeypatch):
    _make_jars(tmp_path, monkeypatch, ["freerouting-2.2.4.jar", "freerouting-1.9.0.jar"])
    jar = find_freerouting_jar(java_version=21)
    assert jar is not None and jar.name == "freerouting-1.9.0.jar"


def test_java25_picks_v2_2(tmp_path, monkeypatch):
    _make_jars(tmp_path, monkeypatch, ["freerouting-2.2.4.jar", "freerouting-1.9.0.jar"])
    jar = find_freerouting_jar(java_version=25)
    assert jar is not None and jar.name == "freerouting-2.2.4.jar"


def test_no_hint_picks_highest(tmp_path, monkeypatch):
    _make_jars(tmp_path, monkeypatch, ["freerouting-2.2.4.jar", "freerouting-1.9.0.jar"])
    jar = find_freerouting_jar()
    assert jar is not None and jar.name == "freerouting-2.2.4.jar"


def test_java21_only_incompatible_returns_none(tmp_path, monkeypatch):
    _make_jars(tmp_path, monkeypatch, ["freerouting-2.2.4.jar"])
    assert find_freerouting_jar(java_version=21) is None
