"""Configuration management using Pydantic Settings."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class BackendType(str, Enum):
    """Backend selector.

    AUTO (the default) is IPC-first for live-board ops: the router in
    PluginDirectBackend serves board reads/writes over the KiCad IPC API
    (kipy) when it is reachable with a loaded board, falls back to the SWIG
    plugin bridge, and degrades to the file backend only when KiCad is closed
    (writes safe-refuse while KiCad is open). The IPC leg can be forced off at
    runtime with KICAD_MCP_IPC_ENABLED=0 (see the ipc_* settings below).
    """

    AUTO = "auto"
    IPC = "ipc"
    SWIG = "swig"
    CLI = "cli"
    FILE = "file"


class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class KiCadMCPConfig(BaseSettings):
    """Configuration for the KiCad MCP server, loaded from environment variables."""

    model_config = {"env_prefix": "KICAD_MCP_", "env_file": ".env", "extra": "ignore"}

    backend: BackendType = Field(
        default=BackendType.AUTO,
        description="Backend to use: auto (= IPC-first live routing with bridge "
                    "fallback), ipc, swig, cli, file",
    )
    # --- IPC board backend (F1) -------------------------------------------
    # These mirror the exact env vars the IPC connection reads at runtime
    # (KICAD_MCP_IPC_ENABLED / _SOCKET / _TIMEOUT_MS via this class's
    # KICAD_MCP_ env prefix), so the config object and the backend can never
    # disagree. Note: ipc_connection.py reads the env directly — the
    # KICAD_MCP_IPC_* names stay authoritative even under the plugin entry
    # point's KICAD_PLUGIN_ prefix.
    ipc_enabled: bool = Field(
        default=True,
        description="Route live-board ops through the KiCad IPC API when "
                    "available. Set KICAD_MCP_IPC_ENABLED=0 to force the SWIG "
                    "bridge / file paths.",
    )
    ipc_socket: Optional[str] = Field(
        default=None,
        description="IPC API socket path override (e.g. ipc://C:/temp/kicad/"
                    "api.sock). Defaults to kipy's resolution: KICAD_API_SOCKET "
                    "env or the platform default under the temp dir.",
    )
    ipc_timeout_ms: int = Field(
        default=2000,
        gt=0,
        description="Per-request timeout for IPC API calls, in milliseconds.",
    )
    transport: TransportType = Field(
        default=TransportType.STDIO,
        description="MCP transport: stdio, sse, or streamable-http",
    )
    log_level: LogLevel = Field(
        default=LogLevel.INFO,
        description="Logging level",
    )
    log_file: Optional[Path] = Field(
        default=None,
        description="Path to log file. Defaults to ~/.kicad-mcp/logs/server.log",
    )
    kicad_cli_path: Optional[Path] = Field(
        default=None,
        description="Explicit path to kicad-cli executable",
    )
    backup_enabled: bool = Field(
        default=True,
        description="Create backups before modifying files",
    )
    backup_dir: Optional[Path] = Field(
        default=None,
        description="Backup directory. Defaults to .kicad_mcp_backups/ next to project",
    )
    change_log_path: Optional[Path] = Field(
        default=None,
        description="Path to change audit log. Defaults to ~/.kicad-mcp/logs/changes.jsonl",
    )
    freerouting_jar: Optional[Path] = Field(
        default=None,
        description="Path to FreeRouting JAR file for auto-routing",
    )
    freerouting_timeout_seconds: int = Field(
        default=300,
        description="Wall-clock limit for one FreeRouting run before it is "
                    "killed. Denser boards legitimately need minutes; "
                    "override via KICAD_MCP_FREEROUTING_TIMEOUT_SECONDS.",
    )
    java_path: Optional[Path] = Field(
        default=None,
        description="Path to java executable for running FreeRouting",
    )
    sse_host: str = Field(
        default="127.0.0.1",
        description="Network bind host for sse / streamable-http transports",
    )
    sse_port: int = Field(
        default=8765,
        description="Network bind port for sse / streamable-http transports",
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.kicad_cli_path is not None:
            p = Path(self.kicad_cli_path)
            if not p.exists():
                raise ValueError(f"kicad-cli not found at: {p}")
            self.kicad_cli_path = p

    def get_data_dir(self) -> Path:
        """Get the platform-appropriate data directory."""
        if os.name == "nt":
            base = Path(os.environ.get("USERPROFILE", Path.home()))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        data_dir = base / ".kicad-mcp"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def get_log_dir(self) -> Path:
        """Get the log directory, creating it if needed."""
        log_dir = self.get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def get_log_file_path(self) -> Path:
        """Resolve the log file path."""
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            return self.log_file
        return self.get_log_dir() / "server.log"

    def get_change_log_path(self) -> Path:
        """Resolve the change log path."""
        if self.change_log_path:
            self.change_log_path.parent.mkdir(parents=True, exist_ok=True)
            return self.change_log_path
        return self.get_log_dir() / "changes.jsonl"
