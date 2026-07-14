"""Unit tests for launcher.settings + the new pure U2 helpers."""

from __future__ import annotations

from pathlib import Path

from launcher import recents, settings
from launcher.config import LauncherConfig, connect_info


def _cfg(tmp_path: Path) -> LauncherConfig:
    return LauncherConfig(
        venv_python=tmp_path / "python.exe",
        venv_pythonw=tmp_path / "pythonw.exe",
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_config_path=tmp_path / ".mcp.dev.json",
        projects_roots=[],
        recents_path=tmp_path / "store" / "recents.json",
    )


# --- settings ---------------------------------------------------------------

def test_defaults_when_missing(tmp_path):
    st = settings.load_settings(_cfg(tmp_path))
    assert st["variant"] == "console"
    assert st["window_x"] is None


def test_save_and_reload(tmp_path):
    cfg = _cfg(tmp_path)
    settings.save_settings(cfg, variant="bento", window_x=120, window_y=40, width_bento=900)
    st = settings.load_settings(cfg)
    assert st["variant"] == "bento"
    assert st["window_x"] == 120
    assert st["width_bento"] == 900
    assert st["width_console"] == settings.DEFAULTS["width_console"]  # untouched


def test_corrupt_file_returns_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    p = cfg.recents_path.parent / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{nope", encoding="utf-8")
    assert settings.load_settings(cfg) == settings.DEFAULTS


def test_invalid_values_sanitized(tmp_path):
    cfg = _cfg(tmp_path)
    settings.save_settings(cfg, variant="weird", window_x="abc")
    st = settings.load_settings(cfg)
    assert st["variant"] == "console"
    assert st["window_x"] is None


def test_width_for(tmp_path):
    st = {"width_console": 520, "width_bento": None}
    assert settings.width_for(st, "console") == 520
    assert settings.width_for(st, "bento") == settings.DEFAULTS["width_bento"]
    assert settings.width_for({"width_console": 100}, "console") == 420  # floor


def test_unknown_keys_not_persisted(tmp_path):
    cfg = _cfg(tmp_path)
    out = settings.save_settings(cfg, evil="x", variant="bento")
    assert "evil" not in out


# --- browse resolution ------------------------------------------------------

def test_resolve_pcb_direct(tmp_path):
    pcb = tmp_path / "a.kicad_pcb"
    pcb.write_text("x", encoding="utf-8")
    assert recents.resolve_board_path(pcb) == pcb


def test_resolve_pro_to_sibling_pcb(tmp_path):
    pro = tmp_path / "a.kicad_pro"
    pcb = tmp_path / "a.kicad_pcb"
    pro.write_text("x", encoding="utf-8")
    pcb.write_text("x", encoding="utf-8")
    assert recents.resolve_board_path(pro) == pcb


def test_resolve_pro_without_pcb_is_none(tmp_path):
    pro = tmp_path / "a.kicad_pro"
    pro.write_text("x", encoding="utf-8")
    assert recents.resolve_board_path(pro) is None


def test_resolve_other_suffix_is_none(tmp_path):
    other = tmp_path / "a.txt"
    other.write_text("x", encoding="utf-8")
    assert recents.resolve_board_path(other) is None


def test_resolve_missing_pcb_is_none(tmp_path):
    assert recents.resolve_board_path(tmp_path / "ghost.kicad_pcb") is None


# --- connect info -----------------------------------------------------------

def test_connect_info_shape(tmp_path):
    info = connect_info(_cfg(tmp_path))
    assert info == {
        "mcpServers": {"kicad": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}
    }


# --- port-aware server matching ----------------------------------------------

def test_matches_server_port_aware():
    from launcher.processes import _matches_server

    srv = ["python", "-m", "kicad_mcp_plugin", "--transport", "streamable-http",
           "--host", "127.0.0.1", "--port", "8765"]
    assert _matches_server(srv, 8765) is True
    assert _matches_server(srv, 8799) is False          # other port -> not ours
    # unspecified --port means the 8765 default
    default = ["python", "-m", "kicad_mcp_plugin", "--transport", "streamable-http"]
    assert _matches_server(default, 8765) is True
    assert _matches_server(default, 8799) is False
    # --port=NNNN form
    eq = ["python", "-m", "kicad_mcp_plugin", "--transport", "streamable-http", "--port=9000"]
    assert _matches_server(eq, 9000) is True
    # not a server at all
    assert _matches_server(["python", "probe.py", "8765"], 8765) is False
    # stdio server (no streamable-http) never matches
    stdio = ["python", "-m", "kicad_mcp_plugin"]
    assert _matches_server(stdio, 8765) is False
