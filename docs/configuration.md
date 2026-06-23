---
layout: default
title: Configuration
nav_order: 4
---

# Configuration
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Environment Variables

```bash
# Transport: stdio, sse
KICAD_MCP_TRANSPORT=stdio

# Logging level: DEBUG, INFO, WARNING, ERROR
KICAD_MCP_LOG_LEVEL=INFO

# Log file path (default: ~/.kicad-mcp/logs/server.log)
KICAD_MCP_LOG_FILE=

# Explicit path to kicad-cli executable
KICAD_MCP_KICAD_CLI_PATH=

# Enable file backups before modification (default: true)
KICAD_MCP_BACKUP_ENABLED=true

# Bridge port (default: 9760)
KICAD_MCP_PLUGIN_PORT=9760

# SSE server settings (only used with --transport sse)
KICAD_MCP_SSE_HOST=127.0.0.1
KICAD_MCP_SSE_PORT=8765
```

## Programmatic Configuration

```python
from kicad_mcp_plugin.config import KiCadPluginConfig
from kicad_mcp_plugin.server import create_plugin_server

config = KiCadPluginConfig(log_level="INFO")
mcp = create_plugin_server(config)
mcp.run(transport="stdio")
```

---

## Troubleshooting

### Does KiCad-MCP require FreeRouting?

**No.** FreeRouting is completely optional and only needed for the 6 auto-routing tools. All other 96 tools work without FreeRouting or Java.

### Subsystem Not Available

Use the `get_backend_info` MCP tool to see routing status. Common fixes:

- **Bridge unreachable**: Run the install script (`install_bridge.ps1` or `install_bridge.sh`), restart pcbnew, call `open_kicad`
- **kicad-cli not found**: Install KiCad and ensure `kicad-cli` is in PATH, or set `KICAD_MCP_KICAD_CLI_PATH`
- **Schematic/library ops always available**: these use the pure Python file backend and require no running KiCad instance

### Known Limitations (Plugin Backend)

- **Board switching**: `open_kicad` polls `get_active_project` for up to 10 s after launching pcbnew with a new board. If the board hasn't finished loading it returns `"bridge": "pending"` — call `open_kicad` again in a few seconds.
- **Bridge reinstall required after source updates**: The installed bridge is a snapshot. Re-run the install script and restart pcbnew after any bridge source changes.

### Large Schematic or Board Responses Truncated

`read_schematic` and `read_board` automatically cap list fields to keep responses within AI token limits. When a list is truncated the response includes metadata:

```json
{
  "symbols": [ ... ],
  "symbols_total": 342,
  "symbols_truncated": true
}
```

Use dedicated per-list tools when you need specific data from a large design:

| Instead of | Use |
|---|---|
| `read_schematic` symbols | `get_symbol_pin_positions` for a specific ref |
| `read_schematic` wires | `get_net_connections` for net analysis |
| `read_board` components | `get_board_info` for counts, `get_design_rules` for rules |

### Logging

Enable debug logging to troubleshoot issues:

```bash
KICAD_MCP_LOG_LEVEL=DEBUG python -m kicad_mcp_plugin
```

Logs are saved to `~/.kicad-mcp/logs/server.log` by default.

---

## Development

### Setup Development Environment

```bash
git clone https://github.com/havamal-65/KiCad-MCP.git
cd KiCad-MCP
pip install -e .[dev]
```

### Run Tests

Unit tests (no KiCad installation required):

```bash
pytest --tb=short -q
```

### Code Quality

```bash
# Format and lint
ruff check .
ruff format .

# Type checking
mypy src
```

### Project Structure

```
KiCad-MCP/
├── src/kicad_mcp/
│   ├── backends/          # plugin_backend, cli_backend, file_backend
│   ├── models/            # Data models and error types
│   ├── resources/         # MCP resources
│   ├── tools/             # MCP tools (board, schematic, export, routing, library, DRC, project, parts)
│   └── utils/             # Utilities (platform detection, sexp parser, validation, parts index)
├── src/kicad_mcp_plugin/
│   ├── backends/
│   │   └── plugin_direct.py  # PluginDirectBackend — explicit routing, no fallbacks
│   ├── config.py          # Plugin entry point config
│   ├── server.py          # MCP server — registers all 102 tools
│   └── __main__.py        # CLI entry point
├── kicad_plugin/
│   ├── kicad_mcp_bridge.py  # KiCad ActionPlugin — TCP bridge
│   └── install_bridge.ps1   # PowerShell installer (Windows)
├── tests/                 # pytest suite (mocked — no live KiCad needed)
├── run_plugin.ps1         # Windows launcher (auto-creates venv)
├── run_plugin.sh          # macOS/Linux launcher
└── pyproject.toml         # Project metadata
```

---

[View Roadmap →](roadmap)
