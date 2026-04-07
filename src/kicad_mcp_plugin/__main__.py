"""CLI entry point: python -m kicad_mcp_plugin"""

from __future__ import annotations

import argparse
import sys

from kicad_mcp import __version__
from kicad_mcp.config import LogLevel, TransportType
from kicad_mcp_plugin.config import KiCadPluginConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "KiCad MCP Plugin Server — primary entry point that routes board ops "
            "through kicad_mcp_bridge running inside KiCad. "
            "The server starts even if KiCad is not yet open; use the open_kicad tool "
            "to launch KiCad, then enable kicad_mcp_bridge to unlock board tools."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"kicad-mcp-plugin {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=None,
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--kicad-cli",
        default=None,
        help="Explicit path to kicad-cli executable (used for DRC/export)",
    )
    parser.add_argument(
        "--sse-host",
        default=None,
        help="SSE server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--sse-port",
        type=int,
        default=None,
        help="SSE server port (default: 8765)",
    )

    args = parser.parse_args()

    overrides: dict = {}
    if args.transport:
        overrides["transport"] = TransportType(args.transport)
    if args.log_level:
        overrides["log_level"] = LogLevel(args.log_level)
    if args.kicad_cli:
        overrides["kicad_cli_path"] = args.kicad_cli
    if args.sse_host:
        overrides["sse_host"] = args.sse_host
    if args.sse_port:
        overrides["sse_port"] = args.sse_port

    config = KiCadPluginConfig(**overrides)

    from kicad_mcp_plugin.server import create_plugin_server
    mcp = create_plugin_server(config)

    if config.transport == TransportType.SSE:
        mcp.run(transport="sse", host=config.sse_host, port=config.sse_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
