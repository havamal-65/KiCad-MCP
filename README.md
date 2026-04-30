# KiCad MCP Server

A pure Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for KiCad EDA automation. Enable AI assistants like Claude, Cursor, and others to interact with KiCad projects programmatically.

## Overview

KiCad MCP Server provides a standardized interface for AI assistants to read, analyze, and modify KiCad electronic design automation (EDA) files. It supports multiple backend implementations to work with KiCad in different environments and use cases.

### Key Features

- **94 MCP Tools** across 9 categories:
  - 📋 **Project Management** (14 tools): Create projects, open projects, list files, read/write metadata, text variable management, query backend info, query active KiCad project via IPC (Linux-safe fallback when `GetOpenDocuments` is unavailable), PCB workflow reference, plan capture and retrieval, **startup gate checklist**
  - 📐 **Schematic Operations** (21 tools): Create schematics from scratch, place/remove/move components, wire routing, labels, no-connects, junctions, power symbols, property editing, pin position queries (with `extends` resolution), net connectivity analysis, hierarchical sheet traversal, schematic-to-PCB comparison and sync
  - 🔌 **PCB Board Operations** (15 tools): Read boards, place/move components, add tracks/vias/board outlines, assign nets, query design rules, refill copper zones, query layer stackup, write IPC-2221/JLCPCB design rules, geometry-driven auto-placement (with utilization reporting), full schematic-to-routed-PCB pipeline (with mandatory pre-flight gate), **diff two board snapshots**
  - 📚 **Library Search** (8 tools): Search symbols/footprints, list libraries, get symbol/footprint info, suggest footprints for a symbol (with physical dimensions), query footprint courtyard dimensions, **estimate board size from footprint list**
  - 📦 **Library Management** (9 tools): Clone repos, register sources, import symbols/footprints, create project libraries
  - ✅ **Design Rule Checks** (8 tools): Run DRC and ERC validations, file-based schematic and board validation (no kicad-cli), kicad-cli strict schematic validation, query board design rules, **pre-sync schematic completeness check**, **fast courtyard overlap check**
  - 📤 **Export Operations** (7 tools): Export Gerbers, drill files, BOMs, pick-and-place, PDFs (with actionable error diagnostics), **3D STEP and VRML models**
  - 🔀 **Auto-Routing** (6 tools): PCB trace auto-routing via FreeRouting (optional), **clear all routes for re-placement**
  - 🔍 **Parts Catalog** (6 tools): Index and search third-party KiCad library sources by MPN, install parts into project libraries

- **Multiple Backend Support**:
  - **Plugin Backend** *(primary on Windows)*: TCP bridge to KiCad's embedded Python — full board read+write via `pcbnew` API, DRC and export via `kicad-cli`, schematics via file backend
  - **IPC Backend**: Direct communication with running KiCad instance via gRPC
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

## Claude Code Skill: `/build-pcb`

When using this server with Claude Code, invoke `/build-pcb [project description]` to
start a **professional, phased PCB design session**. The skill mirrors IPC/JEDEC
industry practice with seven gated phases and a report + user confirmation between each:

| Phase | Name | Gate condition |
|-------|------|---------------|
| 1 | Environment & Requirements | `get_startup_checklist.ready_for_pcb` |
| 2 | Schematic Capture | All components placed, ≥1 net |
| 3 | Schematic Verification | `validate_schematic_for_pcb.ready_for_pcb_sync`, ERC clean |
| 4 | PCB Setup & Placement | `check_courtyard_overlaps.passed` |
| 5 | Routing | Zero unrouted connections |
| 6 | Design Verification | `run_drc.passed` |
| 7 | Manufacturing Outputs | All six export files generated |

After each phase Claude prints a `## Phase N Report` block and pauses for your
confirmation before continuing. Hard gates prevent routing over courtyard overlaps or
syncing a schematic with blocking issues.

---

## Quick Start

### Plugin Entry Point (recommended on Windows with KiCad 9)

The plugin entry point routes board operations through the in-KiCad TCP bridge (`kicad_mcp_bridge`) and is the primary supported path for live PCB work.

**Prerequisites**: Install the bridge plugin and restart KiCad (see [Plugin Backend Setup](#plugin-backend-setup) below).

```bash
python -m kicad_mcp_plugin
```

### Run the Server

#### Stdio Transport (for Claude Desktop, Cursor, etc.)

```bash
python -m kicad_mcp_plugin
```

#### SSE Transport (for web clients)

```bash
python -m kicad_mcp_plugin --transport sse --sse-host 127.0.0.1 --sse-port 8765
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

The repo ships two sets of bootstrap scripts:

- **`run.ps1` / `run.sh`** — legacy composite entry point (`kicad_mcp`)
- **`run_plugin.ps1` / `run_plugin.sh`** — plugin entry point (`kicad_mcp_plugin`, recommended on Windows with KiCad 9)

Both automatically create a virtual environment and install all dependencies on first run, and they clear inherited `PYTHONHOME` / `PYTHONPATH` overrides so MCP clients use the repo's venv instead of global Python packages.

### Codex CLI

Register the server once with `codex mcp add`:

Windows:

```powershell
codex mcp add kicad `
  --env KICAD_MCP_BACKEND=auto `
  --env KICAD_MCP_LOG_LEVEL=INFO `
  -- "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -ExecutionPolicy Bypass `
  -NonInteractive `
  -File "C:\path\to\KiCad-MCP\run.ps1"
```

macOS / Linux:

```bash
codex mcp add kicad \
  --env KICAD_MCP_BACKEND=auto \
  --env KICAD_MCP_LOG_LEVEL=INFO \
  -- /path/to/KiCad-MCP/run.sh
```

Verify with:

```bash
codex mcp get kicad
```

### Claude Code (recommended)

A `.mcp.json` is included at the repo root. Claude Code picks it up automatically when you open the folder, so no manual config is required. It uses the plugin entry point (`kicad_mcp_plugin`) by default, which requires KiCad to be open with the bridge installed.

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

Plugin entry point (recommended — requires bridge installed and pcbnew open):
```json
{
  "mcpServers": {
    "kicad": {
      "command": "/path/to/KiCad-MCP/run_plugin.sh",
      "env": {
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Legacy composite entry point (no KiCad required for file/CLI ops):
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
python -m kicad_mcp_plugin
```

## Available Tools

### Project Management (14 tools)
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
- `plan_project`: Record a structured project plan into a `project_plan.json` file. When `board_width_mm`/`board_height_mm` are 0 (default) and footprint IDs are included in `key_components`, auto-estimates board dimensions from courtyard bounds. Emits a `size_warning` when provided dimensions are more than 15% smaller than the estimate.
- `read_project_plan`: Read back the saved project plan for a given project directory
- `get_startup_checklist`: Run a six-item PASS/FAIL gate before any board operation: KiCad running · bridge reachable · bridge version · PCB editor open · kicad-cli on PATH · active project loaded. Returns `ready_for_pcb` bool and `required_actions` list. **Must be called at the start of every session involving PCB operations.**

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

### PCB Board Operations (15 tools)
- `read_board`: Read complete board structure
- `get_board_info`: Get board metadata (title, revision, layers, counts)
- `place_component`: Place a component footprint on the board
- `move_component`: Move an existing component to a new position
- `add_track`: Add a copper track segment
- `add_via`: Add a via (through-hole, blind, or buried)
- `add_board_outline`: Add or replace the Edge.Cuts board outline with a rectangle at the specified origin and size. When called via `pcb_pipeline`, the board is automatically centered at the KiCad canvas origin (0, 0) so it always appears in the middle of the work area.
- `assign_net`: Assign a net to a component pad
- `get_design_rules`: Get the board's design rules (clearances, track widths, via sizes)
- `refill_zones`: Refill all copper pour zones on a board
- `get_stackup`: Get the layer stackup definition for a board
- `set_board_design_rules`: Write manufacturing-enforceable design rules into the `.kicad_pro` `net_settings.classes` Default entry. Preset `"class2"` applies IPC-2221 Class 2 / IPC-7351 Level B values (0.20 mm clearance, 0.25 mm trace, 0.30 mm via drill). Preset `"fab_jlcpcb"` applies JLCPCB 2-layer standard rules.
- `auto_place`: Geometry-driven bin-packing placement. Reads the courtyard extents for every footprint, sorts by component class (connectors → ICs → discretes → transistors → LEDs → others), and packs components into rows with a guaranteed courtyard-to-courtyard gap ≥ `clearance_mm`. Returns `utilization_pct` (courtyard area / board area) and warns when >70%.
- `diff_board`: Detect changes between two PCB board snapshots. Compares component positions and track counts between two `.kicad_pcb` files. Returns `added_components`, `removed_components`, `moved_components`, and `track_delta`. Useful for confirming `autoroute` added tracks or `auto_place` moved all components.
- `pcb_pipeline`: Full schematic-to-routed-PCB pipeline in a single call. Step 0 runs a mandatory pre-flight gate (startup checklist + `validate_schematic_for_pcb` + board-size estimate); Steps 1–6: `sync_schematic_to_pcb` → `set_board_design_rules` → add Edge.Cuts outline (centered at origin) → `auto_place` → **courtyard overlap check** (fails pipeline if overlaps present) → `autoroute` → `run_drc`. Pipeline aborts with a clear error if any gate fails.

### Library Search (8 tools)
- `search_symbols`: Search for schematic symbols across installed libraries
- `search_footprints`: Search for PCB footprints across installed libraries
- `list_libraries`: List all available symbol and footprint libraries
- `get_symbol_info`: Get detailed information about a specific symbol
- `get_footprint_info`: Get detailed information about a specific footprint
- `suggest_footprints`: Suggest matching footprints for a symbol based on its footprint filters (searches all installed footprint libraries). Each result includes `width_mm`, `height_mm`, and `area_mm2` so you can make size-aware selections.
- `get_footprint_bounds`: Get the courtyard bounding box (`xmin`, `ymin`, `xmax`, `ymax`), `width_mm`, `height_mm`, and pad list for any footprint before placing it. Use this to compute non-overlapping placement positions.
- `estimate_board_size`: Calculate minimum board dimensions from a list of footprint IDs before calling `plan_project`. Sums courtyard areas, adds routing overhead (default 20%), edge clearance (default 3 mm per side), rounds to the nearest 5 mm fab grid, and applies a final dimensional margin (default 25%). Returns `recommended_width_mm`, `recommended_height_mm`, and a per-component breakdown. **Call this before `plan_project` — never guess board size.**

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

### Design Rule Checks (8 tools)
- `run_drc`: Run Design Rule Check on a PCB board
- `run_erc`: Run Electrical Rules Check on a schematic
- `validate_schematic`: File-based electrical rules validation (no kicad-cli required)
- `validate_schematic_cli`: Validate schematic loadability using kicad-cli's strict C++ symbol loader. Exercises symbol geometry and `extends` chain resolution that the Python API accepts but kicad-cli export may reject. Returns `{"passed": bool, "backend": "kicad-cli", "message": "..."}` or `{"status": "unavailable"}` when kicad-cli is not installed.
- `validate_board`: File-based pre-flight checks for a PCB board (no kicad-cli required). Checks: Edge.Cuts outline present (error), duplicate reference designators (error), footprints at (0, 0) (warning), design rules block absent (warning). Returns `{"passed": bool, "violations": [...], "error_count": n, "warning_count": n}`.
- `get_board_design_rules`: Get the design rules configured for a board
- `validate_schematic_for_pcb`: Pre-sync completeness check (no kicad-cli required). Verifies every component has a Footprint, references are unique, PWR_FLAG symbols cover power nets, no component sits at (0, 0), net count is non-zero, and optionally runs full ERC if kicad-cli is available. Returns `ready_for_pcb_sync` bool and a `blocking_issues` list. **Must pass before calling `sync_schematic_to_pcb`.**
- `check_courtyard_overlaps`: Fast file-based courtyard AABB intersection check (milliseconds, no kicad-cli). Returns `passed` bool and a list of overlapping component pairs with `overlap_x_mm`, `overlap_y_mm`, and `suggested_move_mm`. **Must pass before calling `autoroute`.**

### Export Operations (7 tools)
- `export_gerbers`: Export Gerber manufacturing files from a PCB board
- `export_drill`: Export drill files (Excellon format)
- `export_bom`: Export Bill of Materials (CSV, JSON, etc.)
- `export_pick_and_place`: Export pick-and-place component placement file
- `export_pdf`: Export a board or schematic to PDF. Verifies kicad-cli is on PATH before attempting export and confirms the output file was actually created. On failure, surfaces the exact kicad-cli command attempted and stderr so you can diagnose the root cause.
- `export_step`: Export a 3D STEP model from a PCB board for mechanical integration. Requires kicad-cli.
- `export_vrml`: Export a 3D VRML model from a PCB board for 3D rendering and simulation tools. Requires kicad-cli.

### Auto-Routing (6 tools) - Optional
**Requires:** [FreeRouting](https://github.com/freerouting/freerouting) and Java

These tools provide automated PCB trace routing capabilities:
- `export_dsn`: Export PCB to Specctra DSN format for FreeRouting
- `import_ses`: Import routed SES session file back into PCB
- `run_freerouter`: Execute FreeRouting auto-router on a DSN file
- `clean_board_for_routing`: Remove keepouts and problematic tracks before routing
- `autoroute`: Complete pipeline (clean → export → route → import)
- `clear_routes`: Remove all routed tracks and vias from a board file, preserving footprint placement, nets, and the board outline. Writes a `.clear_routes_backup.kicad_pcb` file before modifying. Use this to re-place and re-route without manual file surgery. If the plugin bridge is active, reloads the board in KiCad automatically.

> **Note**: The auto-routing tools are completely optional. All other KiCad-MCP functionality works without FreeRouting or Java.

### Parts Catalog (6 tools)
These tools index and search third-party KiCad library sources (GitHub releases, local directories) and install parts by MPN into project libraries. They extend the built-in Library Management tools with a content-addressed parts index.

- `list_known_sources`: List all well-known third-party KiCad library sources (name, URL, type, description)
- `bootstrap_known_source`: Download and register a well-known source by name (clones repo or extracts archive into `~/.kicad-mcp/sources/<name>/`, then registers it)
- `index_library_source`: Build or rebuild the parts index for a registered source (scans `.kicad_sym` / `.kicad_mod` files, extracts MPN and manufacturer fields)
- `search_parts`: Search the parts index by MPN, value, description, or manufacturer across all indexed sources. Returns ranked matches with symbol and footprint paths.
- `install_part`: Copy a part from an indexed source into a project-local library by MPN. Installs both symbol and footprint if available.
- `parts_index_stats`: Report index statistics for all registered sources (symbol count, footprint count, last indexed time, source path)

## Backend Details

### Plugin Entry Point Backend Routing

`kicad_mcp_plugin` (the recommended entry point on Windows) uses `PluginDirectBackend` with fixed routing — no auto-detection fallbacks:

| Operation | Backend |
|-----------|---------|
| Board read/write (place, move, track, via, zones, outline, DSN/SES) | Plugin bridge (TCP → `pcbnew`) |
| Schematic read/write | File backend |
| DRC / export (Gerbers, drill, BOM, PDF, STEP, VRML) | kicad-cli |
| Library search / management | File backend |

### Backend Capabilities

| Feature | Plugin | IPC | SWIG | CLI | File |
|---------|--------|-----|------|-----|------|
| Board Read | ✅ | ✅ | ✅ | ✅ | ✅ |
| Board Modify | ✅ | ✅ | ✅ | ⚠️ | ❌ |
| Export | ✅¹ | ✅ | ✅ | ✅ | ❌ |
| DRC/ERC | ✅¹ | ✅ | ✅ | ✅ | ❌ |
| Schematic Read | ✅² | ✅ | ❌ | ⚠️ | ✅ |
| Schematic Write | ✅² | ✅ | ❌ | ⚠️ | ❌ |
| Live KiCad | ✅ | ✅ | ❌ | ❌ | ❌ |
| No KiCad Required | ❌ | ❌ | ❌ | ❌ | ✅ |

⚠️ = Limited support  
¹ Plugin entry point routes export/DRC to kicad-cli  
² Plugin entry point routes schematic ops to file backend (read/write)

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
│   ├── backends/          # Backend implementations (composite, plugin, CLI, SWIG, file, IPC)
│   ├── models/            # Data models and error types
│   ├── resources/         # MCP resources
│   ├── tools/             # MCP tools (board, schematic, export, routing, library, DRC, project)
│   ├── utils/             # Utilities (platform detection, sexp parser, validation)
│   ├── config.py          # Configuration
│   └── server.py          # MCP server setup
├── src/kicad_mcp_plugin/
│   ├── backends/
│   │   └── plugin_direct.py  # PluginDirectBackend — explicit routing, no fallbacks
│   ├── config.py          # Plugin entry point config (KICAD_PLUGIN_ env prefix)
│   ├── server.py          # Plugin MCP server setup
│   └── __main__.py        # CLI entry point: python -m kicad_mcp_plugin
├── kicad_plugin/
│   ├── kicad_mcp_bridge.py  # KiCad ActionPlugin — TCP bridge (installed into KiCad)
│   └── install_bridge.ps1   # PowerShell installer (Windows, PowerShell 7+)
├── examples/
│   ├── air_quality_sensor/  # Complete worked example (schematic build script)
│   ├── wearable_aqs/        # Wearable AQS (full schematic + routed PCB, E2E verified)
│   └── usb_c_power_breakout_20260406_try3/  # USB-C breakout (pcb_pipeline E2E, plugin backend)
├── run_plugin.ps1         # Windows launcher for kicad_mcp_plugin (auto-creates venv)
├── run_plugin.sh          # macOS/Linux launcher for kicad_mcp_plugin
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

### Wearable Air Quality Sensor (complete routed PCB)

`examples/wearable_aqs/` contains a complete KiCad project with schematic, routed PCB, and exported manufacturing files.

**BOM**: ESP32-C3-WROOM-02 (WiFi/BLE MCU) · SCD41 (CO₂/temp/humidity) · SGP41 (VOC/NOx) · AMS1117-3.3 (LDO regulator) · USB-C connector · JST battery connector · decoupling capacitors

**Board**: 60 × 50 mm, 2-layer, JLCPCB rules, 231 tracks, DRC 0 errors

**What it demonstrates**:
- Full `pcb_pipeline` workflow (schematic → auto-place → autoroute → DRC)
- `set_board_design_rules` with `"fab_jlcpcb"` preset written to `.kicad_pro`
- `auto_place` geometry-driven bin-packing with ESP32 courtyard parsed from `fp_line` segments
- `autoroute` via FreeRouting — complete routing in under 10 seconds
- `export_gerbers`, `export_drill`, `export_bom` — manufacturing-ready output in `manufacturing/`

### USB-C Power Breakout (plugin backend E2E)

`examples/usb_c_power_breakout_20260406_try3/` contains a complete board created end-to-end via the plugin entry point, with the `kicad_mcp_bridge` providing live `pcbnew` access throughout.

**BOM**: USB-C connector · AMS1117-3.3 LDO · decoupling capacitors · protection diode · status LED

**Board**: ~40 × 25 mm, 2-layer, 6 footprints, 6 nets, copper routed, DRC clean

**What it demonstrates**:
- `pcb_pipeline` end-to-end via the plugin backend (`pcbnew` TCP bridge for all board ops)
- `drc_passed: true` on a fully plugin-driven board
- BOM export via `export_bom`

## Troubleshooting

### Does KiCad-MCP require FreeRouting?

**No.** FreeRouting is completely optional and only needed if you want to use the 6 auto-routing tools. All other 88 tools work without FreeRouting or Java.

If you try to use auto-routing tools without FreeRouting, you'll get a helpful error message with download instructions.

### Plugin Backend Setup

The plugin backend gives the MCP direct live access to `pcbnew`'s in-memory board data while KiCad is open, with no gRPC overhead. It works on **Windows, Linux, and macOS** with KiCad 9.

The install scripts:
1. Remove any stale bridge copies from `scripting/plugins/` (these cause a `sys.modules` conflict that silently prevents the bridge from starting)
2. Install `kicad_mcp_bridge.py` as `__init__.py` in KiCad's PCM plugins directory
3. Patch `pcbnew.json` so KiCad auto-loads the bridge on every pcbnew startup

**Windows (PowerShell 7+):**

```powershell
pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1
```

Installs to: `[MyDocuments]\KiCad\9.0\3rdparty\plugins\kicad_mcp_bridge\`

**Linux / macOS:**

```bash
bash kicad_plugin/install_bridge.sh
```

Installs to:
- Linux: `$XDG_DATA_HOME/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/` (default: `~/.local/share/kicad/9.0/…`)
- macOS: `~/Library/Preferences/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/`

**After installing (all platforms):**
1. Close all KiCad / pcbnew windows
2. Open pcbnew and load your board
3. Verify the bridge is running:
   - Windows: `Test-NetConnection -ComputerName localhost -Port 9760`
   - Linux/macOS: `python3 -c "import socket; s=socket.create_connection(('localhost',9760),2); print('bridge OK'); s.close()"`
4. Start the MCP server: `python -m kicad_mcp_plugin`

**Reinstalling after source updates:** Re-run the install script, then close and reopen pcbnew. Check `bridge_startup.log` in the plugin directory for startup diagnostics.

**Port configuration:** `KICAD_MCP_PLUGIN_PORT` env var (default `9760`).

### Known Limitations (Plugin Backend)

- **Board switching**: After calling `open_kicad` with a new board path, the bridge stays connected to the previously open board. You must manually open the new board in pcbnew before bridge operations will reflect the new board.
- **Bridge reinstall required after source updates**: The installed bridge (`3rdparty/plugins/kicad_mcp_bridge/__init__.py`) is a snapshot. Re-run the install script and restart pcbnew after any bridge source changes.

### Backend Not Available

Use `get_backend_info` MCP tool to see which backends are active. Install missing dependencies:

- Plugin: Run `install_bridge.ps1` (Windows) or `install_bridge.sh` (Linux/macOS), restart pcbnew, then use `python -m kicad_mcp_plugin`
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
KICAD_MCP_LOG_LEVEL=DEBUG python -m kicad_mcp_plugin
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
