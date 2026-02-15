# KiCad MCP Server

A pure Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for KiCad EDA automation. Enable AI assistants like Claude, Cursor, and others to interact with KiCad projects programmatically.

## Overview

KiCad MCP Server provides a standardized interface for AI assistants to read, analyze, and modify KiCad electronic design automation (EDA) files. It supports multiple backend implementations to work with KiCad in different environments and use cases.

### Key Features

- **47 MCP Tools** across 7 categories:
  - 📋 **Project Management** (5 tools): Create, validate, and manage KiCad projects
  - 📐 **Schematic Operations** (12 tools): Read, modify, and analyze schematics with pin position queries, no-connects, junctions, power symbols, component removal, and repositioning
  - 🔌 **PCB Board Operations** (8 tools): Work with PCB layouts, components, tracks, and nets
  - 📚 **Library Search** (5 tools): Query and inspect component/footprint libraries
  - 📦 **Library Management** (9 tools): Clone repos, register sources, import symbols/footprints, create project libraries
  - ✅ **Design Rule Checks** (3 tools): Run DRC and ERC validations
  - 📤 **Export Operations** (5 tools): Export schematics, boards, BOMs, and more

- **Multiple Backend Support**:
  - **IPC Backend**: Direct communication with running KiCad instance
  - **SWIG Backend**: Native Python bindings (requires `kicad-python`)
  - **CLI Backend**: Uses `kicad-cli` command-line tool
  - **File Backend**: Pure Python file parsing (no KiCad installation required)

- **Smart Backend Selection**: Automatically detects and uses the best available backend
- **Change Tracking**: Built-in logging of all operations for debugging and auditing
- **Backup Support**: Automatic file backups before modifications
- **Flexible Configuration**: Environment variables, CLI args, or programmatic config

## Installation

### Requirements

- Python 3.10 or higher
- KiCad 7.0+ (optional, depending on backend)

### From PyPI (Coming Soon)

```bash
pip install kicad-mcp
```

### From Source

```bash
git clone https://github.com/havamal-65/KiCad-MCP.git
cd KiCad-MCP
pip install -e .
```

### Optional Dependencies

For IPC backend (direct KiCad communication):
```bash
pip install kicad-mcp[ipc]
```

For development:
```bash
pip install kicad-mcp[dev]
```

## Quick Start

### Check Available Backends

```bash
python -m kicad_mcp --check
```

This will show:
- Platform information
- Python version
- KiCad CLI availability and version
- Status of each backend

### Run the Server

#### Stdio Transport (for Claude Desktop, Cursor, etc.)

```bash
python -m kicad_mcp
```

#### SSE Transport (for web clients)

```bash
python -m kicad_mcp --transport sse --sse-host 127.0.0.1 --sse-port 8765
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and customize:

```bash
# Backend selection: auto, ipc, swig, cli, file
KICAD_MCP_BACKEND=auto

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

# SSE server settings (only used with --transport sse)
KICAD_MCP_SSE_HOST=127.0.0.1
KICAD_MCP_SSE_PORT=8765
```

### Command-Line Options

```bash
python -m kicad_mcp --help
```

Options:
- `--transport {stdio,sse}`: MCP transport method
- `--backend {auto,ipc,swig,cli,file}`: Backend selection
- `--log-level {DEBUG,INFO,WARNING,ERROR}`: Logging verbosity
- `--kicad-cli PATH`: Custom path to kicad-cli
- `--sse-host HOST`: SSE server host
- `--sse-port PORT`: SSE server port
- `--check`: Check backend availability and exit

### Programmatic Configuration

```python
from kicad_mcp.config import BackendType, KiCadMCPConfig
from kicad_mcp.server import create_server

config = KiCadMCPConfig(
    backend=BackendType.AUTO,
    log_level="INFO",
)

mcp = create_server(config)
mcp.run(transport="stdio")
```

## Client Integration

### Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "python",
      "args": ["-m", "kicad_mcp"],
      "env": {
        "KICAD_MCP_BACKEND": "auto",
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "python",
      "args": ["-m", "kicad_mcp"],
      "env": {
        "KICAD_MCP_BACKEND": "auto",
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

## Available Tools

### Project Management
- `create_project`: Create a new KiCad project
- `validate_project`: Validate project structure
- `list_project_files`: List all files in a project
- `get_project_metadata`: Get project information
- `update_project_metadata`: Update project settings

### Schematic Operations (12 tools)
- `read_schematic`: Read complete schematic structure (symbols, wires, labels, no-connects, junctions)
- `add_component`: Place symbols with rotation, mirror, footprint, and custom properties
- `add_wire`: Draw wire connections
- `add_label`: Add net labels (net, global, hierarchical)
- `get_symbol_pin_positions`: Get absolute schematic coordinates for each pin of a placed symbol (essential for wire routing)
- `add_no_connect`: Add no-connect (X) markers to unused pins
- `add_power_symbol`: Add power symbols (+3V3, GND, VCC, etc.) with auto-incrementing references
- `add_junction`: Add junction dots at wire intersections
- `remove_component`: Remove a placed component by reference designator
- `move_schematic_component`: Move a component to a new position with optional rotation (shifts property labels too)
- `annotate_schematic`: Auto-annotate component reference designators
- `generate_netlist`: Generate netlist from schematic

### PCB Board Operations
- `read_board`: Read complete board structure
- `get_board_info`: Get board metadata
- `list_board_components`: List footprints
- `get_board_component`: Get footprint details
- `update_board_component`: Modify footprint properties
- `list_tracks`: List all tracks
- `list_board_nets`: List PCB nets
- `get_copper_zones`: Get copper fill zones

### Library Management
- `list_libraries`: List available libraries
- `search_library`: Search for components
- `get_library_info`: Get library metadata
- `list_library_components`: List components in a library
- `get_library_component`: Get component details

### Design Rule Checks
- `run_erc`: Run electrical rule check on schematic
- `run_drc`: Run design rule check on board
- `get_netlist`: Generate and validate netlist

### Export Operations
- `export_schematic`: Export schematic (PDF, SVG, PNG)
- `export_board`: Export PCB (PDF, SVG, PNG, Gerber)
- `export_bom`: Generate bill of materials
- `export_netlist`: Export netlist
- `export_step`: Export 3D STEP model

## Backend Details

### Backend Priority

When using `auto` backend selection, the server tries backends in this order:

1. **IPC** - Fastest, requires running KiCad instance
2. **SWIG** - Fast, requires kicad-python package
3. **CLI** - Moderate, requires kicad-cli tool
4. **File** - Slowest, pure Python parsing

### Backend Capabilities

| Feature | IPC | SWIG | CLI | File |
|---------|-----|------|-----|------|
| Read Files | ✅ | ✅ | ✅ | ✅ |
| Modify Files | ✅ | ✅ | ⚠️ | ⚠️ |
| Export | ✅ | ✅ | ✅ | ❌ |
| DRC/ERC | ✅ | ✅ | ✅ | ❌ |
| Live KiCad | ✅ | ❌ | ❌ | ❌ |
| No KiCad Required | ❌ | ❌ | ❌ | ✅ |

⚠️ = Limited support

## Development

### Setup Development Environment

```bash
git clone https://github.com/havamal-65/KiCad-MCP.git
cd KiCad-MCP
pip install -e .[dev]
```

### Run Tests

```bash
pytest
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
│   ├── backends/          # Backend implementations
│   ├── models/            # Data models
│   ├── resources/         # MCP resources
│   ├── tools/             # MCP tools
│   ├── utils/             # Utilities
│   ├── config.py          # Configuration
│   ├── server.py          # MCP server setup
│   └── __main__.py        # CLI entry point
├── tests/                 # Test suite
├── examples/              # Usage examples
├── pyproject.toml         # Project metadata
└── README.md
```

## Use Cases

- **AI-Assisted PCB Design**: Let AI assistants help design and review circuits
- **Automated Quality Checks**: Run DRC/ERC as part of CI/CD pipelines
- **Batch Processing**: Automate repetitive design tasks across multiple projects
- **Design Analysis**: Extract and analyze design data programmatically
- **Documentation Generation**: Auto-generate BOMs, netlists, and design docs
- **Design Migration**: Convert or update designs programmatically

## Troubleshooting

### Backend Not Available

Run `python -m kicad_mcp --check` to see which backends are available. Install missing dependencies:

- IPC: Requires KiCad to be running
- SWIG: `pip install kicad-mcp[ipc]`
- CLI: Install KiCad and ensure `kicad-cli` is in PATH
- File: Always available (pure Python)

### Logging

Enable debug logging to troubleshoot issues:

```bash
python -m kicad_mcp --log-level DEBUG
```

Logs are saved to `~/.kicad-mcp/logs/server.log` by default.

## License

MIT License - See LICENSE file for details

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Run the test suite and linters
5. Submit a pull request

## Acknowledgments

- Built with [FastMCP](https://github.com/jlowin/fastmcp)
- Powered by [KiCad](https://www.kicad.org/)
- Inspired by the [Model Context Protocol](https://modelcontextprotocol.io)

## Links

- **GitHub**: https://github.com/havamal-65/KiCad-MCP
- **Issues**: https://github.com/havamal-65/KiCad-MCP/issues
- **KiCad**: https://www.kicad.org/
- **MCP Documentation**: https://modelcontextprotocol.io
