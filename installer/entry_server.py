"""Frozen entry point: the MCP server (packaged app, U4).

The launcher spawns this sibling executable in place of the dev path's
``<venv python> -m kicad_mcp_plugin`` (see launcher/processes.start_mcp_http).
"""
from kicad_mcp_plugin.__main__ import main

if __name__ == "__main__":
    main()
