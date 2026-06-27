"""set_board_design_rules must work with the bridge DOWN (pcbnew closed).

It only edits the sibling .kicad_pro / .kicad_dru files — no in-memory pcbnew
state — so the plugin server exempts it from the bridge guard. Gating it behind
the bridge made it unusable in exactly the situation where it is safest from the
file-vs-pcbnew clobber (pcbnew closed). Regression for that fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp_plugin.config import KiCadPluginConfig
from kicad_mcp_plugin.server import create_plugin_server


def _bridge_down(*args, **kwargs):
    raise OSError("bridge down")


_MIN_PRO = {
    "board": {"design_settings": {"rules": {}}},
    "meta": {"filename": "b.kicad_pro", "version": 3},
    "net_settings": {"classes": [], "meta": {"version": 4},
                     "net_colors": None, "netclass_assignments": None,
                     "netclass_patterns": []},
}


def _project(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240101) (generator t))", encoding="utf-8")
    (tmp_path / "b.kicad_pro").write_text(json.dumps(_MIN_PRO, indent=2) + "\n", encoding="utf-8")
    return pcb


def _tool(mcp, name):
    return next(t.fn for t in mcp._tool_manager._tools.values() if t.name == name)


@pytest.fixture()
def server_bridge_down(monkeypatch):
    # Bridge unreachable for both the backend probe and the guard's ping.
    monkeypatch.setattr("kicad_mcp.backends.plugin_backend._tcp_call", _bridge_down)
    mcp = create_plugin_server(KiCadPluginConfig())
    monkeypatch.setattr("kicad_mcp_plugin.server._tcp_call", _bridge_down)
    return mcp


def test_set_board_design_rules_runs_with_bridge_down(server_bridge_down, tmp_path):
    pcb = _project(tmp_path)
    out = json.loads(_tool(server_bridge_down, "set_board_design_rules")(
        str(pcb), "fab_jlcpcb",
        differential_pairs=[{"name": "USB", "nets": ["USB_D+", "USB_D-"],
                             "width_mm": 0.20, "gap_mm": 0.13}],
    ))
    # File-side write succeeded despite the bridge being down.
    assert out["status"] == "success", out
    pro = json.loads((tmp_path / "b.kicad_pro").read_text(encoding="utf-8"))
    assert any(c["name"] == "USB" for c in pro["net_settings"]["classes"])
    # And the clobber-hazard advisory is surfaced.
    assert "coherence_note" in out
    assert "pcbnew" in out["coherence_note"]


def test_genuinely_board_bound_tool_still_guarded(server_bridge_down, tmp_path):
    # A real board tool (needs pcbnew) still returns the bridge-down response.
    pcb = _project(tmp_path)
    out = json.loads(_tool(server_bridge_down, "read_board")(str(pcb)))
    assert "bridge" in json.dumps(out).lower()
    assert out.get("status") != "success"
