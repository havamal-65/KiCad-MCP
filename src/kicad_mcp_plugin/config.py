"""Configuration for the plugin MCP entry point.

Inherits all fields from KiCadMCPConfig but uses a separate env prefix
(KICAD_PLUGIN_) so that environment variables from the legacy server don't
bleed into the plugin server and vice versa.

BackendType is not exposed — the plugin server always uses plugin + file + cli.
"""

from __future__ import annotations

from kicad_mcp.config import KiCadMCPConfig


class KiCadPluginConfig(KiCadMCPConfig):
    """Config for the plugin entry point."""

    model_config = {  # type: ignore[assignment]
        "env_prefix": "KICAD_PLUGIN_",
        "env_file": ".env.plugin",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
