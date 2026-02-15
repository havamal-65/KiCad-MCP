"""CLI entry point: python -m kicad_mcp"""

from __future__ import annotations

import argparse
import sys

from kicad_mcp import __version__
from kicad_mcp.config import BackendType, KiCadMCPConfig, LogLevel, TransportType


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KiCad MCP Server - Pure Python MCP server for KiCad EDA automation",
    )
    parser.add_argument(
        "--version", action="version", version=f"kicad-mcp {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=None,
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "ipc", "swig", "cli", "file"],
        default=None,
        help="Backend to use (default: auto-detect)",
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
        help="Explicit path to kicad-cli executable",
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
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check available backends and exit",
    )

    args = parser.parse_args()

    if args.check:
        _check_backends()
        return

    # Build config from CLI args + env vars
    overrides = {}
    if args.transport:
        overrides["transport"] = TransportType(args.transport)
    if args.backend:
        overrides["backend"] = BackendType(args.backend)
    if args.log_level:
        overrides["log_level"] = LogLevel(args.log_level)
    if args.kicad_cli:
        overrides["kicad_cli_path"] = args.kicad_cli
    if args.sse_host:
        overrides["sse_host"] = args.sse_host
    if args.sse_port:
        overrides["sse_port"] = args.sse_port

    config = KiCadMCPConfig(**overrides)

    from kicad_mcp.server import create_server
    mcp = create_server(config)

    if config.transport == TransportType.SSE:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


def _check_backends() -> None:
    """Print available backend information and exit."""
    from kicad_mcp.backends.factory import get_available_backends
    from kicad_mcp.utils.platform_helper import get_platform_info

    print(f"KiCad MCP Server v{__version__}")
    print()

    platform_info = get_platform_info()
    print(f"Platform: {platform_info['platform']}")
    print(f"Python: {platform_info['python_version'].split()[0]}")
    print(f"KiCad CLI: {platform_info['kicad_cli'] or 'not found'}")
    print(f"KiCad Version: {platform_info['kicad_version'] or 'unknown'}")
    print()

    print("Backend Availability:")
    backends = get_available_backends()
    for name, info in backends.items():
        status = "available" if info["available"] else "not available"
        version = f" (v{info['version']})" if info.get("version") else ""
        print(f"  {name:6s}: {status}{version}")


if __name__ == "__main__":
    main()
