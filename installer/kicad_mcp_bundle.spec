# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — the packaged KiCad-MCP app (U4, REQ-U4-001/002).

Two executables sharing ONE _internal runtime (single COLLECT):
- "KiCad-MCP Launcher.exe"  — windowed pywebview app (launcher/)
- "kicad-mcp-server.exe"    — console MCP server (kicad_mcp_plugin), spawned
                              by the launcher as a sibling process when frozen

Every collect below encodes a live spike finding (spec-u4 §1): fastmcp's
docket drags fakeredis+lupa dynamically; kipy/pynng, kicad-skip and uvicorn
import dynamically; the launcher loads its UI assets and the health monitor
by file path. tkinter is deliberately ABSENT (guarded import in the monitor).
"""

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

REPO = Path(SPECPATH).resolve().parent  # noqa: F821 — SPECPATH is a PyInstaller global
SRC = REPO / "src"

with open(REPO / "pyproject.toml", "rb") as fh:
    APP_VERSION = tomllib.load(fh)["project"]["version"]

pathex = [str(REPO), str(SRC)]

# ── shared dynamic-import surface (spike findings 1+2) ───────────────────────
datas: list = []
binaries: list = []
hiddenimports: list = ["sniffio"]
for package in ("kipy", "pynng", "skip", "lupa", "fakeredis"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("kicad_mcp")
hiddenimports += collect_submodules("kicad_mcp_plugin")
for dist in ("fastmcp", "mcp", "pydantic"):
    datas += copy_metadata(dist)

# Bridge payload — the wheel's force-include location, which both the Python
# bridge installer (importlib.resources) and setup_core resolve when frozen.
datas += [(str(REPO / "kicad_plugin" / "kicad_mcp_bridge.py"),
           "kicad_mcp_plugin/data")]

# ── launcher-only surface (spike findings 3; GUI backends) ───────────────────
launcher_datas = list(datas)
launcher_binaries = list(binaries)
launcher_hidden = list(hiddenimports) + collect_submodules("pystray") \
    + collect_submodules("PIL") + collect_submodules("launcher")
for package in ("webview", "clr_loader", "pythonnet"):
    d, b, h = collect_all(package)
    launcher_datas += d
    launcher_binaries += b
    launcher_hidden += h
launcher_datas += [
    (str(REPO / "launcher" / "ui"), "launcher/ui"),
    (str(REPO / "scripts" / "mcp_health_monitor.py"), "scripts"),
]

a_launcher = Analysis(  # noqa: F821
    [str(REPO / "installer" / "entry_launcher.py")],
    pathex=pathex,
    binaries=launcher_binaries,
    datas=launcher_datas,
    hiddenimports=launcher_hidden,
    excludes=["tkinter"],
)
a_server = Analysis(  # noqa: F821
    [str(REPO / "installer" / "entry_server.py")],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)

pyz_launcher = PYZ(a_launcher.pure)  # noqa: F821
pyz_server = PYZ(a_server.pure)  # noqa: F821

exe_launcher = EXE(  # noqa: F821
    pyz_launcher,
    a_launcher.scripts,
    [],
    exclude_binaries=True,
    name="KiCad-MCP Launcher",
    console=False,
    disable_windowed_traceback=False,
)
exe_server = EXE(  # noqa: F821
    pyz_server,
    a_server.scripts,
    [],
    exclude_binaries=True,
    name="kicad-mcp-server",
    console=True,  # launcher spawns it with CREATE_NO_WINDOW; console aids debugging
)

COLLECT(  # noqa: F821
    exe_launcher,
    a_launcher.binaries,
    a_launcher.datas,
    exe_server,
    a_server.binaries,
    a_server.datas,
    name="KiCad-MCP Launcher",
)
