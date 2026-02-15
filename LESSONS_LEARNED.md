# KiCad MCP: Lessons Learned & Enhancement Requirements

Compiled from hands-on experience building the Air Quality Sensor schematic and PCB using the MCP tools.

## Lessons Learned

### 1. Pin Position Calculation Was the Hardest Part (FIXED)

**Problem**: When placing wires, we needed to know the exact coordinates of each pin endpoint. The MCP had no way to query pin positions — we had to manually calculate them from library symbol definitions, applying coordinate transforms (Y-flip, rotation matrix).

**Fix Applied**: Added `get_symbol_pin_positions` tool and enriched `get_symbol_info` with pin position data (Change 1 & 2 in the recent update).

### 2. No-Connects, Junctions, and Power Symbols Had No Tool Support (FIXED)

**Problem**: The air quality schematic needed 13 no-connect markers, 13 power symbols, and several junctions. All had to be hand-written as raw S-expressions.

**Fix Applied**: Added `add_no_connect`, `add_power_symbol`, `add_junction` tools (Change 4).

### 3. Component Placement Lacked Footprint/Rotation/Properties (FIXED)

**Problem**: `add_component` only took lib_id, reference, value, x, y. Real schematics need footprint assignments, rotation, mirror, and custom properties (Datasheet, MPN).

**Fix Applied**: Enriched `add_component` with rotation, mirror, footprint, properties params (Change 3).

### 4. Schematic Read Missed Key Elements (FIXED)

**Problem**: `read_schematic` returned symbols, wires, labels — but not no-connects, junctions, power symbol flags, or footprint assignments. This made it impossible to understand existing schematics fully.

**Fix Applied**: Enriched `read_schematic` to return no_connects, junctions, is_power, footprint (Change 5).

### 5. Reference Designator Sync Between Schematic and PCB Is Error-Prone

**Problem**: Found duplicate U5 references in the PCB — an old pin header footprint and the correct TP4056 charger both had the same designator. This caused KiCad to prompt for 3 items to place on PCB editor entry. Root cause: manual edits or partial "Update PCB from Schematic" runs leaving stale footprints.

**Impact**: Confusing PCB editor behavior, potential for wrong netlist connections.

**Enhancement Needed**: A tool to detect reference designator mismatches between schematic and PCB, and a tool to run "Update PCB from Schematic" programmatically.

### 6. No Schematic-to-PCB Sync Tool

**Problem**: After modifying the schematic, there's no MCP tool to sync changes to the PCB. Users must open KiCad GUI and run "Update PCB from Schematic" manually.

**Enhancement Needed**: `sync_schematic_to_pcb` tool or at minimum a `compare_schematic_pcb` diagnostic tool.

### 7. No Way to Delete or Modify Placed Components (PARTIALLY FIXED)

**Problem**: When a component is placed incorrectly (wrong reference, wrong position, stale footprint), there's no tool to remove or update it. Can only add.

**Fix Applied (partial)**: Added `remove_component` and `move_schematic_component` tools. Both use text-level S-expression block finding (via `find_symbol_block_by_reference()`) to locate the exact symbol instance while skipping the `lib_symbols` cache section. The move tool also shifts all property label positions by the same delta to keep them aligned.

**Remaining**: `update_component_property` tool still needed.

### 8. Library Symbol Cache Management

**Problem**: When adding symbols to a schematic via file manipulation, the `lib_symbols` section must contain the symbol definition for KiCad to render it. Power symbols especially need their lib_symbols cache entry. Currently `add_power_symbol` just inserts the instance and warns if the cache is missing.

**Enhancement Needed**: Auto-populate `lib_symbols` cache when adding components, or provide a `refresh_lib_symbols_cache` tool.

### 9. No Hierarchical Sheet Support

**Problem**: Complex designs use hierarchical sheets. The MCP only reads/modifies the root schematic file. Sub-sheets are invisible.

**Enhancement Needed**: Support for reading and navigating hierarchical sheets.

### 10. Footprint-to-Symbol Mapping Is Manual

**Problem**: When placing a component, the user must know the exact footprint lib_id string. No tool helps find the right footprint for a given symbol.

**Enhancement Needed**: `suggest_footprints(symbol_lib_id)` tool that reads the symbol's default footprint filter.

### 11. No Net-Aware Operations

**Problem**: Can't query "what net is this pin on?" or "show me all connections to net +3V3". Wire routing is blind — connect points and hope the netlist is correct.

**Enhancement Needed**: `get_net_connections(path, net_name)`, `get_pin_net(path, reference, pin)` tools.

### 12. No ERC/DRC from MCP After Modifications

**Problem**: After programmatically building a schematic, can't run ERC to verify correctness without opening KiCad. DRC similarly requires CLI backend.

**Enhancement Needed**: Ensure ERC/DRC tools work with file backend, or provide a validation tool that checks common issues (floating pins, missing connections).

---

## Enhancement Requirements (Prioritized)

### P0 — Critical for Usable Schematic Automation

| # | Enhancement | Status | Why |
|---|------------|--------|-----|
| 1 | `compare_schematic_pcb` | Planned | Detect ref mismatches, missing footprints, stale components between sch/pcb |
| 2 | `remove_component` (schematic) | **DONE** | Can't fix mistakes without delete capability |
| 3 | `move_schematic_component` (schematic) | **DONE** | Repositioning without delete+re-add |
| 4 | `update_component_property` | Planned | Change footprint, value, or custom props on existing symbols |
| 5 | Auto-populate `lib_symbols` cache | Planned | Components added via MCP should be renderable in KiCad immediately |

### P1 — Important for Practical Workflows

| # | Enhancement | Why |
|---|------------|-----|
| 6 | `sync_schematic_to_pcb` | Programmatic "Update PCB from Schematic" |
| 7 | `suggest_footprints(lib_id)` | Help users find correct footprint for a symbol |
| 8 | `get_net_connections` / `get_pin_net` | Net-aware queries for intelligent wire routing |
| 9 | `remove_wire` / `remove_no_connect` | Undo/fix wiring mistakes |
| 10 | Hierarchical sheet read support | Real projects use sub-sheets |

### P2 — Nice to Have

| # | Enhancement | Why |
|---|------------|-----|
| 11 | `validate_schematic` (file-based ERC lite) | Check floating pins, unconnected nets without KiCad CLI |
| 12 | `auto_place_components` | Suggest initial component placement based on connectivity |
| 13 | `add_text` / `add_graphic` (schematic) | Annotations like "AIRFLOW ->" on the PCB |
| 14 | Batch operations | Place multiple components/wires in one call for performance |
| 15 | Undo/redo support | Track changes and allow rollback beyond file backups |
