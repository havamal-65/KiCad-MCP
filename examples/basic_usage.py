"""Basic usage example for the KiCad MCP Server.

This shows how to create and run the server programmatically.
For CLI usage, simply run: python -m kicad_mcp
"""

from kicad_mcp.config import BackendType, KiCadMCPConfig
from kicad_mcp.server import create_server


def main():
    # Create config - can also be set via environment variables
    config = KiCadMCPConfig(
        backend=BackendType.AUTO,  # Auto-detect available backends
        log_level="INFO",
    )

    # Create and run the server
    mcp = create_server(config)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
