# KiCad MCP Server

A pure Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for KiCad EDA automation. Enable AI assistants like Claude, Cursor, and others to interact with KiCad projects programmatically.

## Overview

KiCad MCP Server provides a standardized interface for AI assistants to read, analyze, and modify KiCad electronic design automation (EDA) files. It supports multiple backend implementations to work with KiCad in different environments and use cases.

### Key Features

- **75 MCP Tools** across 8 categories:
- 📋 **Project Management** (11 tools): Create projects, open projects, list files, read/write metadata, text variable management, query backend info, query active KiCad project via IPC (Linux-safe fallback when `GetOpenDocuments` is unavailable), PCB workflow reference
  - 📐 **Schematic Operations** (21 tools): Create schematics from scratch, place/remove/move components, wire routing, labels, no-connects, junctions, power symbols, property editing, pin position queries (with `extends` resolution), net connectivity analysis, hierarchical sheet traversal, schematic-to-PCB comparison and sync
  - 🔌 **PCB Board Operations** (13 tools): Read boards, place/move components, add tracks/vias, assign nets, query design rules, refill copper zones, query layer stackup, write IPC-2221/JLCPCB design rules, geometry-driven auto-placement, full schematic-to-routed-PCB pipeline
  - 📚 **Library Search** (7 tools): Search symbols/footprints, list libraries, get symbol/footprint info, suggest footprints for a symbol, query footprint courtyard dimensions
  - 📦 **Library Management** (9 tools): Clone repos, register sources, import symbols/footprints, create project libraries
  - ✅ **Design Rule Checks** (4 tools): Run DRC and ERC validations, file-based schematic validation, query board design rules
  - 📤 **Export Operations** (5 tools): Export Gerbers, drill files, BOMs, pick-and-place, PDFs
  - 🔀 **Auto-Routing** (5 tools): PCB trace auto-routing via FreeRouting (optional, requires FreeRouting)

- **Multiple Backend Support**:
  - **IPC Backend**: Direct communication with running KiCad instance
  - **SWIG Backend**: Native Python bindings (requires `kicad-python`)
  - **CLI Backend**: Uses `kicad-cli` command-line tool
  - **File Backend**: Pure Python file parsing (no KiCad installation required)

- **Smart Backend Selection**: Automatically detects and uses the best available backend
- **Safe Response Sizes**: Large list results (symbols, wires, tracks, components) are automatically capped to prevent AI token-limit errors. Truncated fields include `<field>_total` and `<field>_truncated` metadata so you always know the full count.
- **Change Tracking**: Built-in logging of all operations for debugging and auditing
- **Backup Support**: Automatic file backups before modifications
- **Flexible Configuration**: Environment variables, CLI args, or programmatic config

## Installation

### Requirements

- Python 3.10 or higher
- KiCad 7.0+ (optional, depending on backend)

### From PyPI (Coming Soon)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install kicad-mcp
```

### From Source

```bash
git clone https://github.com/havamal-65/KiCad-MCP.git
cd KiCad-MCP
pip install -e .
```

### Optional Dependencies

Activate the venv first, then install the extras inside it:

```bash
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

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

The repo ships bootstrap scripts (`run.ps1` for Windows, `run.sh` for macOS/Linux) that automatically create a virtual environment and install all dependencies on first run — no manual setup needed.

### Claude Code (recommended)

A `.mcp.json` is included at the repo root. Claude Code picks it up automatically when you open the folder, so no manual config is required.

### Claude Desktop — Windows

Add to `%APPDATA%\Claude\claude_desktop_config.json`:
Use an absolute PowerShell path to avoid `Executable not found in $PATH: "powershell"` startup errors.

```json
{
  "mcpServers": {
    "kicad": {
      "command": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "args": [
        "-ExecutionPolicy", "Bypass",
        "-NonInteractive",
        "-File", "C:\\path\\to\\KiCad-MCP\\run.ps1"
      ],
      "env": {
        "KICAD_MCP_BACKEND": "auto",
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Claude Desktop — macOS / Linux

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `~/.config/Claude/claude_desktop_config.json` (Linux):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/path/to/KiCad-MCP/run.sh",
      "env": {
        "KICAD_MCP_BACKEND": "auto",
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings (use `run.ps1` on Windows, `run.sh` on macOS/Linux as above).

### Manual setup (advanced)

If you prefer to manage your own venv:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
python -m kicad_mcp
```

## Available Tools

### Project Management (11 tools)
- `open_kicad`: Launch KiCad (IPC backend only)
- `open_project`: Open a KiCad project and return its structure
- `list_project_files`: List all KiCad-related files in a project directory
- `get_project_metadata`: Read detailed metadata from a KiCad project file
- `save_project`: Trigger save for an open KiCad project (requires IPC backend)
- `get_backend_info`: Get information about available backends and their capabilities
- `get_active_project`: Query the currently open KiCad project and documents (requires IPC backend; on Linux IPC builds without `GetOpenDocuments`, falls back to active board document metadata)
- `get_text_variables`: Get all project-level text variables (`${VAR}` substitutions)
- `set_text_variables`: Set one or more project-level text variables
- `create_project`: Create a new KiCad project with blank `.kicad_pro`, `.kicad_sch`, and `.kicad_pcb` files
- `get_pcb_workflow`: Return a structured 11-step PCB design workflow reference (JSON) showing the recommended tool sequence from project creation through DRC

### Schematic Operations (21 tools)
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
- `compare_schematic_pcb`: Detect mismatches between schematic and PCB (missing components, footprint/value differences)
- `sync_schematic_to_pcb`: Synchronize schematic components to the PCB (auto-place missing, update values, sync pin nets to pad net assignments)
- `annotate_schematic`: Auto-annotate component reference designators
- `generate_netlist`: Generate netlist from schematic

### PCB Board Operations (13 tools)
- `read_board`: Read complete board structure
- `get_board_info`: Get board metadata (title, revision, layers, counts)
- `place_component`: Place a component footprint on the board
- `move_component`: Move an existing component to a new position
- `add_track`: Add a copper track segment
- `add_via`: Add a via (through-hole, blind, or buried)
- `assign_net`: Assign a net to a component pad
- `get_design_rules`: Get the board's design rules (clearances, track widths, via sizes)
- `refill_zones`: Refill all copper pour zones on a board
- `get_stackup`: Get the layer stackup definition for a board
- `set_board_design_rules`: Write manufacturing-enforceable design rules into the board's `(setup ...)` section. Preset `"class2"` applies IPC-2221 Class 2 / IPC-7351 Level B values (0.20 mm clearance, 0.25 mm trace, 0.30 mm via drill). Preset `"fab_jlcpcb"` applies JLCPCB 2-layer standard rules.
- `auto_place`: Geometry-driven bin-packing placement. Reads the courtyard extents for every footprint, sorts by component class (connectors → ICs → discretes → transistors → LEDs → others), and packs components into rows with a guaranteed courtyard-to-courtyard gap ≥ `clearance_mm`.
- `pcb_pipeline`: Full schematic-to-routed-PCB pipeline in a single call: `sync_schematic_to_pcb` → `set_board_design_rules` → add Edge.Cuts outline → `auto_place` → `autoroute` → `run_drc`.

### Library Search (7 tools)
- `search_symbols`: Search for schematic symbols across installed libraries
- `search_footprints`: Search for PCB footprints across installed libraries
- `list_libraries`: List all available symbol and footprint libraries
- `get_symbol_info`: Get detailed information about a specific symbol
- `get_footprint_info`: Get detailed information about a specific footprint
- `suggest_footprints`: Suggest matching footprints for a symbol based on its footprint filters (searches all installed footprint libraries)
- `get_footprint_bounds`: Get the courtyard bounding box (`xmin`, `ymin`, `xmax`, `ymax`), `width_mm`, `height_mm`, and pad list for any footprint before placing it. Use this to compute non-overlapping placement positions.

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

### Design Rule Checks (4 tools)
- `run_drc`: Run Design Rule Check on a PCB board
- `run_erc`: Run Electrical Rules Check on a schematic
- `validate_schematic`: File-based electrical rules validation (no kicad-cli required)
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

Unit tests (no KiCad installation required):

```bash
pytest
```

Integration tests (exercises MCP tools against real KiCad files in a temp directory):

```bash
python tests/integration/run_integration_tests.py
```

The integration test creates a complete KiCad project from scratch using MCP tools, exercises every tool, and prints a PASS / SKIP / FAIL summary. FreeRouting tools auto-detect the JAR from `~/.kicad-mcp/freerouting/`; all 5 export tools require `kicad-cli` and are gracefully skipped if not found.

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
├── tests/
│   ├── integration/       # End-to-end tool tests (run_integration_tests.py)
│   └── *.py               # Unit tests (191 tests)
├── examples/
│   └── air_quality_sensor/  # Complete worked example
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

**No.** FreeRouting is completely optional and only needed if you want to use the 5 auto-routing tools. All other 70 tools work without FreeRouting or Java.

If you try to use auto-routing tools without FreeRouting, you'll get a helpful error message with download instructions.

### Backend Not Available

Run `python -m kicad_mcp --check` to see which backends are available. Install missing dependencies:

- IPC: Requires KiCad to be running
- SWIG: activate the venv then `pip install kicad-mcp[ipc]`
- CLI: Install KiCad and ensure `kicad-cli` is in PATH
- File: Always available (pure Python)

### Linux IPC Project Discovery

Some Linux KiCad IPC builds (for example KiCad 9.0.7) do not implement the `GetOpenDocuments` handler. In that case:

- `get_active_project` still returns project information using `kicad.get_board().document.project`
- `get_text_variables` / `set_text_variables` also fall back to the active board document's project object
- `open_documents` may include only the active PCB document when schematic/project document enumeration is not available

### Auto-Routing Not Working

Auto-routing requires both Java and FreeRouting:

1. Install Java Runtime Environment (JRE)
2. Download [FreeRouting JAR](https://github.com/freerouting/freerouting/releases)
3. Either:
   - Place the JAR in `~/.kicad-mcp/freerouting/`
   - Set `KICAD_MCP_FREEROUTING_JAR` environment variable to JAR path
   - Provide `freerouting_jar` parameter to the tool

The tool will auto-detect Java and FreeRouting if properly installed.

### Large Schematic or Board Responses Truncated

`read_schematic` and `read_board` automatically cap list fields (symbols, wires, components, tracks, etc.) to keep responses within AI token limits. When a list is truncated the response includes sibling metadata keys:

```json
{
  "symbols": [ ... ],
  "symbols_total": 342,
  "symbols_truncated": true
}
```

Use the dedicated per-list tools instead of `read_*` when you need specific data from a large design:

| Instead of | Use |
|---|---|
| `read_schematic` symbols | `get_symbol_pin_positions` for a specific ref |
| `read_schematic` wires | `get_net_connectivity` for net analysis |
| `read_board` components | `get_board_info` for counts, `get_design_rules` for rules |

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
