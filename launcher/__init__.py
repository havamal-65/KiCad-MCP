"""KiCad-MCP Stack Launcher (UI).

A standalone Tkinter desktop launcher that brings up the whole stack — KiCad
(pcbnew) + the MCP server + Claude Code — for a picked project, shows live
status by reusing the existing health monitor's collectors, and owns the MCP
server lifecycle (start / stop / restart).

Import contract (REQ-PLAT-003): this package's non-GUI core modules
(`config`, `recents`, `orchestrator`, `processes`, `status`) MUST NOT import
`tkinter` at module load, so they import and unit-test on headless CI. Only
`app` and `__main__` touch Tk.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
