# KiCad MCP Server

A pure Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for KiCad EDA automation. Enable AI assistants like Claude, Cursor, and others to interact with KiCad projects programmatically.

## Overview

KiCad MCP Server provides a standardized interface for AI assistants to read, analyze, and modify KiCad electronic design automation (EDA) files. It supports multiple backend implementations to work with KiCad in different environments and use cases.

### Key Features

- **64 MCP Tools** across 8 categories:
  - 📋 **Project Management** (6 tools): Open projects, list files, read/write metadata, query backend info, query active KiCad project via IPC
  - 📐 **Schematic Operations** (22 tools): Create schematics from scratch, place/remove/move components, wire routing, labels, no-connects, junctions, power symbols, property editing, pin position queries (with `extends` resolution), net connectivity analysis, hierarchical sheet traversal, file-based schematic validation, schematic-to-PCB comparison and sync
  - 🔌 **PCB Board Operations** (8 tools): Read boards, place/move components, add tracks/vias, assign nets, query design rules
  - 📚 **Library Search** (6 tools): Search symbols/footprints, list libraries, get symbol/footprint info, suggest footprints for a symbol
  - 📦 **Library Management** (9 tools): Clone repos, register sources, import symbols/footprints, create project libraries
  - ✅ **Design Rule Checks** (3 tools): Run DRC and ERC validations
  - 📤 **Export Operations** (5 tools): Export Gerbers, drill files, BOMs, pick-and-place, PDFs
  - 🔀 **Auto-Routing** (5 tools): PCB trace auto-routing via FreeRouting (optional, requires FreeRouting)

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

For auto-routing functionality (requires Java and FreeRouting):
- Download [FreeRouting JAR](https://github.com/freerouting/freerouting/releases)
- Ensure Java Runtime Environment (JRE) is installed
- Set `KICAD_MCP_FREEROUTING_JAR` and `KICAD_MCP_JAVA_PATH` environment variables (or let the tool auto-detect)

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

### Project Management (6 tools)
- `open_project`: Open a KiCad project and return its structure
- `list_project_files`: List all KiCad-related files in a project directory
- `get_project_metadata`: Read detailed metadata from a KiCad project file
- `save_project`: Trigger save for an open KiCad project (requires IPC backend)
- `get_backend_info`: Get information about available backends and their capabilities
- `get_active_project`: Query the currently open KiCad project and documents (requires IPC backend)

### Schematic Operations (22 tools)
- `read_schematic`: Read complete schematic structure (symbols, wires, labels, no-connects, junctions)
- `create_schematic`: Create a new, empty KiCad 8+ schematic file with proper structure
- `add_component`: Place symbols with rotation, mirror, footprint, custom properties, and KiCad 8+ instance data
- `add_wire`: Draw wire connections between two points
- `add_label`: Add net labels (net, global, hierarchical)
- `add_no_connect`: Add no-connect (X) markers to unused pins
- `add_power_symbol`: Add power symbols (+3V3, GND, VCC, etc.) with auto-incrementing references
- `add_junction`: Add junction dots at wire intersections
- `remove_component`: Remove a placed component by reference designator
- `remove_wire`: Remove a wire segment by its endpoint coordinates
- `remove_no_connect`: Remove a no-connect marker by its position
- `move_schematic_component`: Move a component to a new position with optional rotation (shifts property labels too)
- `update_component_property`: Update or add a property (Value, Footprint, MPN, etc.) on a placed component
- `get_symbol_pin_positions`: Get absolute schematic coordinates for each pin of a placed symbol; resolves `extends` chains so symbols like ATtiny85-20S and AMS1117-3.3 work correctly
- `get_pin_net`: Get the net name connected to a specific pin of a symbol
- `get_net_connections`: Get all connections (pins, labels, wires) on a named net
- `get_sheet_hierarchy`: Get the hierarchical sheet tree from a root schematic
- `validate_schematic`: Run file-based electrical rules validation (no kicad-cli required)
- `compare_schematic_pcb`: Detect mismatches between schematic and PCB (missing components, footprint/value differences)
- `sync_schematic_to_pcb`: Synchronize schematic components to the PCB (auto-place missing, update values)
- `annotate_schematic`: Auto-annotate component reference designators
- `generate_netlist`: Generate netlist from schematic

### PCB Board Operations (8 tools)
- `read_board`: Read complete board structure
- `get_board_info`: Get board metadata (title, revision, layers, counts)
- `place_component`: Place a component footprint on the board
- `move_component`: Move an existing component to a new position
- `add_track`: Add a copper track segment
- `add_via`: Add a via (through-hole, blind, or buried)
- `assign_net`: Assign a net to a component pad
- `get_design_rules`: Get the board's design rules (clearances, track widths, via sizes)

### Library Search (6 tools)
- `search_symbols`: Search for schematic symbols across installed libraries
- `search_footprints`: Search for PCB footprints across installed libraries
- `list_libraries`: List all available symbol and footprint libraries
- `get_symbol_info`: Get detailed information about a specific symbol
- `get_footprint_info`: Get detailed information about a specific footprint
- `suggest_footprints`: Suggest matching footprints for a symbol based on its footprint filters

### Library Management (9 tools)
- `clone_library_repo`: Clone a remote KiCad library repository
- `register_library_source`: Register a local directory as a searchable library source
- `list_library_sources`: List all registered external library sources
- `unregister_library_source`: Remove a library source registration
- `search_library_sources`: Search for symbols/footprints across registered external sources
- `create_project_library`: Create an empty project-local KiCad library
- `import_symbol`: Copy a symbol from one .kicad_sym library to another
- `import_footprint`: Copy a footprint from one .pretty directory to another
- `register_project_library`: Register a library in a project's sym-lib-table or fp-lib-table

### Design Rule Checks (3 tools)
- `run_drc`: Run Design Rule Check on a PCB board
- `run_erc`: Run Electrical Rules Check on a schematic
- `get_board_design_rules`: Get the design rules configured for a board

### Export Operations (5 tools)
- `export_gerbers`: Export Gerber manufacturing files from a PCB board
- `export_drill`: Export drill files (Excellon format)
- `export_bom`: Export Bill of Materials (CSV, JSON, etc.)
- `export_pick_and_place`: Export pick-and-place component placement file
- `export_pdf`: Export a board or schematic to PDF

### Auto-Routing (5 tools) - Optional
**Requires:** [FreeRouting](https://github.com/freerouting/freerouting) and Java

These tools provide automated PCB trace routing capabilities:
- `export_dsn`: Export PCB to Specctra DSN format for FreeRouting
- `import_ses`: Import routed SES session file back into PCB
- `run_freerouter`: Execute FreeRouting auto-router on a DSN file
- `clean_board_for_routing`: Remove keepouts and problematic tracks before routing
- `autoroute`: Complete pipeline (clean → export → route → import)

> **Note**: The auto-routing tools are completely optional. All other KiCad-MCP functionality works without FreeRouting or Java.

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

## Examples

### Air Quality Sensor (end-to-end test)

`examples/air_quality_sensor/` contains a complete worked example that builds a real schematic from scratch using only the file backend — no KiCad installation required.

**BOM**: ATtiny85-20S (MCU) · SCD41 (CO₂/temp/humidity) · SGP41 (VOC/NOx) · AMS1117-3.3 (LDO) · 2× 4.7 kΩ pullups · 6× decoupling caps · power connector · I2C debug header

**What it exercises**:
- `create_schematic` — create a new KiCad 8+ schematic
- `add_component` — place all 14 components (including symbols from a custom `.kicad_sym`)
- `get_symbol_pin_positions` — query exact pin coordinates (resolves `extends` chains automatically)
- `add_power_symbol` — annotate power rails (+5V, +3V3, GND) at each power pin
- `add_label` — apply SDA / SCL net labels
- `add_no_connect` — mark unused pins

```bash
python examples/air_quality_sensor/build_schematic.py
```

The script also demonstrates how to inject a custom symbol library into the file backend's search path, making the SCD41 and SGP41 symbols (not in the standard KiCad library) available to all MCP tools.

## Troubleshooting

### Does KiCad-MCP require FreeRouting?

**No.** FreeRouting is completely optional and only needed if you want to use the 5 auto-routing tools. All other 59 tools work without FreeRouting or Java.

If you try to use auto-routing tools without FreeRouting, you'll get a helpful error message with download instructions.

### Backend Not Available

Run `python -m kicad_mcp --check` to see which backends are available. Install missing dependencies:

- IPC: Requires KiCad to be running
- SWIG: `pip install kicad-mcp[ipc]`
- CLI: Install KiCad and ensure `kicad-cli` is in PATH
- File: Always available (pure Python)

### Auto-Routing Not Working

Auto-routing requires both Java and FreeRouting:

1. Install Java Runtime Environment (JRE)
2. Download [FreeRouting JAR](https://github.com/freerouting/freerouting/releases)
3. Either:
   - Place the JAR in `~/.kicad-mcp/freerouting/`
   - Set `KICAD_MCP_FREEROUTING_JAR` environment variable to JAR path
   - Provide `freerouting_jar` parameter to the tool

The tool will auto-detect Java and FreeRouting if properly installed.

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
