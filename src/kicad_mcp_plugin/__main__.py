"""CLI entry point: python -m kicad_mcp_plugin"""

from __future__ import annotations

import argparse
import sys
from typing import Any

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
        choices=["stdio", "sse", "streamable-http"],
        default=None,
        help="MCP transport (default: stdio). Use streamable-http (or sse) for "
             "hot-reload dev mode — see src/CLAUDE.md.",
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
    # --host/--port are the network bind for sse / streamable-http.
    # --sse-host/--sse-port are kept as back-compat aliases.
    parser.add_argument(
        "--host", "--sse-host",
        dest="host",
        default=None,
        help="Network bind host for sse/streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", "--sse-port",
        dest="port",
        type=int,
        default=None,
        help="Network bind port for sse/streamable-http (default: 8765)",
    )

    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.transport:
        overrides["transport"] = TransportType(args.transport)
    if args.log_level:
        overrides["log_level"] = LogLevel(args.log_level)
    if args.kicad_cli:
        overrides["kicad_cli_path"] = args.kicad_cli
    if args.host:
        overrides["sse_host"] = args.host
    if args.port:
        overrides["sse_port"] = args.port

    config = KiCadPluginConfig(**overrides)

    from kicad_mcp_plugin.server import create_plugin_server
    mcp = create_plugin_server(config)

    if config.transport == TransportType.STREAMABLE_HTTP:
        # Served at http://<host>:<port>/mcp — point a dev .mcp.json at that URL.
        mcp.run(
            transport="streamable-http",
            host=config.sse_host,
            port=config.sse_port,
            path="/mcp",
        )
    elif config.transport == TransportType.SSE:
        mcp.run(transport="sse", host=config.sse_host, port=config.sse_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
