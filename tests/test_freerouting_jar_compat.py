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


# --- F13 (fuller): find_java prefers the newest installed JDK ----------------

from pathlib import Path

from kicad_mcp.utils import platform_helper as ph


def test_find_java_prefers_newest_version(monkeypatch):
    """A newer JDK not on PATH must win over an older PATH java (so FreeRouting
    can use its v2.2.x JAR, which needs Java 25)."""
    j21 = Path("/fake/jdk21/bin/java")
    j25 = Path("/fake/jdk25/bin/java")
    monkeypatch.setattr(ph, "_java_candidates", lambda: [j21, j25])
    monkeypatch.setattr(
        ph, "detect_java_major_version", lambda p: 21 if "jdk21" in str(p) else 25
    )
    assert ph.find_java() == j25


def test_find_java_single_candidate_skips_probe(monkeypatch):
    only = Path("/fake/jdk21/bin/java")
    monkeypatch.setattr(ph, "_java_candidates", lambda: [only])

    def _boom(p):
        raise AssertionError("should not probe a single candidate")

    monkeypatch.setattr(ph, "detect_java_major_version", _boom)
    assert ph.find_java() == only


def test_find_java_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(ph, "_java_candidates", lambda: [])
    assert ph.find_java() is None


def test_java_candidates_discovers_dot_jdks(tmp_path, monkeypatch):
    """A JetBrains-managed JDK under ~/.jdks is discovered even when not on PATH."""
    exe = "java.exe" if ph.get_platform() == "windows" else "java"
    jdk_bin = tmp_path / ".jdks" / "jdk-25.0.3+9" / "bin"
    jdk_bin.mkdir(parents=True)
    (jdk_bin / exe).write_bytes(b"x")

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(ph.shutil, "which", lambda name: None)  # no PATH java

    cands = ph._java_candidates()
    assert any(".jdks" in str(c) and c.name == exe for c in cands)
