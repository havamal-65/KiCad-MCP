---
layout: default
title: Tools Reference
nav_order: 3
---

# Tools Reference
{: .no_toc }

102 MCP tools across 9 categories.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Project Management (14 tools)

- **`open_kicad`** — Launch KiCad and wait for the bridge to become ready
- **`open_project`** — Open a KiCad project and return its structure
- **`list_project_files`** — List all KiCad-related files in a project directory
- **`get_project_metadata`** — Read detailed metadata from a KiCad project file
- **`save_project`** — Trigger save for an open KiCad project (requires bridge)
- **`get_backend_info`** — Get information about available backends and their capabilities
- **`get_active_project`** — Query the currently open KiCad project and board path (requires bridge)
- **`get_text_variables`** — Get all project-level text variables (`${VAR}` substitutions)
- **`set_text_variables`** — Set one or more project-level text variables
- **`create_project`** — Create a new KiCad project with blank `.kicad_pro`, `.kicad_sch`, and `.kicad_pcb` files
- **`get_pcb_workflow`** — Return a structured 11-step PCB design workflow reference
- **`plan_project`** — Record a structured project plan; auto-estimates board dimensions when footprint IDs are provided
- **`read_project_plan`** — Read back the saved project plan for a given project directory
- **`get_startup_checklist`** — Run a six-item PASS/FAIL gate before any board operation. **Must be called at the start of every session involving PCB operations.**

---

## Schematic Operations (26 tools)

- **`read_schematic`** — Read complete schematic structure (symbols, wires, labels, no-connects, junctions)
- **`create_schematic`** — Create a new, empty KiCad 8+ schematic file
- **`add_component`** — Place symbols with rotation, mirror, footprint, and custom properties
- **`add_components`** — **Bulk** — place N components in one call
- **`add_wire`** — Draw wire connections between two points
- **`add_label`** — Add net labels (net, global, hierarchical)
- **`add_no_connect`** — Add no-connect markers to unused pins
- **`add_no_connects`** — **Bulk** — mark N unused pins in one call
- **`add_power_symbol`** — Add power symbols (+3V3, GND, VCC, etc.)
- **`add_power_symbols`** — **Bulk** — place N power symbols in one call
- **`connect_pins`** — **Bulk** — net N pins together with stub-and-label connectivity
- **`add_junction`** — Add junction dots at wire intersections
- **`remove_component`** — Remove a placed component by reference designator
- **`remove_wire`** — Remove a wire segment by its endpoint coordinates
- **`remove_no_connect`** — Remove a no-connect marker by its position
- **`move_schematic_component`** — Move a component to a new position with optional rotation
- **`move_components`** — **Bulk** — reposition N components in one call
- **`update_component_property`** — Update or add a property (Value, Footprint, MPN, etc.)
- **`get_symbol_pin_positions`** — Get absolute schematic coordinates for each pin of a placed symbol
- **`get_pin_net`** — Get the net name connected to a specific pin of a symbol
- **`get_net_connections`** — Get all connections (pins, labels, wires) on a named net
- **`get_sheet_hierarchy`** — Get the hierarchical sheet tree from a root schematic
- **`compare_schematic_pcb`** — Detect mismatches between schematic and PCB
- **`sync_schematic_to_pcb`** — Synchronize schematic components to the PCB; reads `PlacementIntent` properties for edge anchoring
- **`annotate_schematic`** — Auto-annotate component reference designators
- **`generate_netlist`** — Generate netlist from schematic

---

## PCB Board Operations (16 tools)

- **`read_board`** — Read complete board structure
- **`get_board_info`** — Get board metadata (title, revision, layers, counts)
- **`place_component`** — Place a component footprint on the board
- **`move_component`** — Move an existing component to a new position
- **`place_at_edge`** — Anchor an edge-facing connector at the named board edge with correct outward rotation
- **`add_track`** — Add a copper track segment
- **`add_via`** — Add a via (through-hole, blind, or buried)
- **`add_board_outline`** — Add or replace the Edge.Cuts board outline
- **`assign_net`** — Assign a net to a component pad
- **`get_design_rules`** — Get the board's design rules (clearances, track widths, via sizes)
- **`refill_zones`** — Refill all copper pour zones on a board
- **`get_stackup`** — Get the layer stackup definition for a board
- **`set_board_design_rules`** — Write manufacturing-enforceable design rules. Presets: `"class2"` (IPC-2221), `"fab_jlcpcb"` (JLCPCB 2-layer)
- **`auto_place`** — Geometry-driven bin-packing placement with sheet-hierarchy clustering and anchor support
- **`diff_board`** — Detect changes between two PCB board snapshots
- **`pcb_pipeline`** — Full schematic-to-routed-PCB pipeline in a single call

---

## Library Search (8 tools)

- **`search_symbols`** — Search for schematic symbols across installed libraries
- **`search_footprints`** — Search for PCB footprints across installed libraries
- **`list_libraries`** — List all available symbol and footprint libraries
- **`get_symbol_info`** — Get detailed information about a specific symbol
- **`get_footprint_info`** — Get detailed information about a specific footprint
- **`suggest_footprints`** — Suggest matching footprints for a symbol; includes physical dimensions
- **`get_footprint_bounds`** — Get the courtyard bounding box for a footprint before placing it
- **`estimate_board_size`** — Calculate minimum board dimensions from a list of footprint IDs

---

## Library Management (9 tools)

- **`clone_library_repo`** — Clone a remote KiCad library repository
- **`register_library_source`** — Register a local directory as a searchable library source
- **`list_library_sources`** — List all registered external library sources
- **`unregister_library_source`** — Remove a library source registration
- **`search_library_sources`** — Search for symbols/footprints across registered external sources
- **`create_project_library`** — Create an empty project-local KiCad library
- **`import_symbol`** — Copy a symbol from one `.kicad_sym` library to another
- **`import_footprint`** — Copy a footprint from one `.pretty` directory to another
- **`register_project_library`** — Register a library in a project's sym-lib-table or fp-lib-table

---

## Design Rule Checks (10 tools)

- **`run_drc`** — Run Design Rule Check on a PCB board
- **`run_erc`** — Run Electrical Rules Check on a schematic
- **`validate_schematic`** — File-based electrical rules validation (no kicad-cli required)
- **`validate_schematic_cli`** — Validate schematic loadability using kicad-cli's strict C++ symbol loader
- **`validate_board`** — File-based pre-flight checks for a PCB board (no kicad-cli required)
- **`get_board_design_rules`** — Get the design rules configured for a board
- **`validate_schematic_for_pcb`** — Pre-sync completeness check. **Must pass before calling `sync_schematic_to_pcb`.**
- **`check_courtyard_overlaps`** — Fast file-based courtyard AABB intersection check. **Must pass before calling `autoroute`.**
- **`identify_edge_facing_connectors`** — Detect connectors that need outward-facing placement at a board edge
- **`validate_connector_orientations`** — Placement-quality gate for edge-facing connectors. **Must pass before calling `autoroute`.**

---

## Export Operations (7 tools)

- **`export_gerbers`** — Export Gerber manufacturing files from a PCB board
- **`export_drill`** — Export drill files (Excellon format)
- **`export_bom`** — Export Bill of Materials (CSV, JSON, etc.)
- **`export_pick_and_place`** — Export pick-and-place component placement file
- **`export_pdf`** — Export a board or schematic to PDF
- **`export_step`** — Export a 3D STEP model for mechanical integration (requires kicad-cli)
- **`export_vrml`** — Export a 3D VRML model for 3D rendering (requires kicad-cli)

---

## Auto-Routing (6 tools)

> **Requires:** [FreeRouting](https://github.com/freerouting/freerouting) and Java. All other 96 tools work without FreeRouting.

- **`export_dsn`** — Export PCB to Specctra DSN format for FreeRouting
- **`import_ses`** — Import routed SES session file back into PCB
- **`run_freerouter`** — Execute FreeRouting auto-router on a DSN file
- **`clean_board_for_routing`** — Remove keepouts and problematic tracks before routing
- **`autoroute`** — Complete pipeline (clean → export → route → import)
- **`clear_routes`** — Remove all routed tracks and vias, preserving footprint placement and nets

---

## Parts Catalog (6 tools)

- **`list_known_sources`** — List all well-known third-party KiCad library sources
- **`bootstrap_known_source`** — Download and register a well-known source by name
- **`index_library_source`** — Build or rebuild the parts index for a registered source
- **`search_parts`** — Search the parts index by MPN, value, description, or manufacturer
- **`install_part`** — Copy a part from an indexed source into a project-local library by MPN
- **`parts_index_stats`** — Report index statistics for all registered sources

---

## Backend Routing Reference

| Operation | Subsystem | KiCad required? |
|-----------|-----------|-----------------|
| Board read/write | TCP bridge → pcbnew | Yes (PCB editor open) |
| Schematic read/write | File backend | No |
| DRC / ERC / export | kicad-cli | Yes (kicad-cli on PATH) |
| Library search/manage | File backend | No |
| Parts catalog | SQLite + HTTP APIs | No |

---

[View Configuration →](configuration)
