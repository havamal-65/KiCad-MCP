"""Configuration management using Pydantic Settings."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class BackendType(str, Enum):
    AUTO = "auto"
    IPC = "ipc"
    SWIG = "swig"
    CLI = "cli"
    FILE = "file"


class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"


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
        description="Backend to use: auto, ipc, swig, cli, file",
    )
    transport: TransportType = Field(
        default=TransportType.STDIO,
        description="MCP transport: stdio or sse",
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
    sse_host: str = Field(default="127.0.0.1", description="SSE server host")
    sse_port: int = Field(default=8765, description="SSE server port")

    def __init__(self, **kwargs):
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
