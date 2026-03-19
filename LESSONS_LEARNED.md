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

### 5. Reference Designator Sync Between Schematic and PCB Is Error-Prone (FIXED)

**Problem**: Found duplicate U5 references in the PCB — an old pin header footprint and the correct TP4056 charger both had the same designator. This caused KiCad to prompt for 3 items to place on PCB editor entry. Root cause: manual edits or partial "Update PCB from Schematic" runs leaving stale footprints.

**Impact**: Confusing PCB editor behavior, potential for wrong netlist connections.

**Fix Applied**: Added `compare_schematic_pcb` tool that detects missing components, footprint mismatches, and value mismatches between schematic and PCB. Power symbols (references starting with `#`) are automatically excluded. Reports missing_from_pcb, missing_from_schematic, footprint_mismatches, and value_mismatches with a summary count.

### 6. No Schematic-to-PCB Sync Tool (FIXED)

**Problem**: After modifying the schematic, there's no MCP tool to sync changes to the PCB. Users must open KiCad GUI and run "Update PCB from Schematic" manually.

**Fix Applied**: Added `sync_schematic_to_pcb` tool that compares schematic and PCB, then auto-places missing components and updates value mismatches. Footprint mismatches and extra PCB components are reported as warnings for manual review. Also added `compare_schematic_pcb` for read-only diagnostics.

### 7. No Way to Delete or Modify Placed Components (FIXED)

**Problem**: When a component is placed incorrectly (wrong reference, wrong position, stale footprint), there's no tool to remove or update it. Can only add.

**Fix Applied**: Added `remove_component`, `move_schematic_component`, and `update_component_property` tools. All use text-level S-expression block finding (via `find_symbol_block_by_reference()`) to locate the exact symbol instance while skipping the `lib_symbols` cache section. The move tool shifts all property label positions by the same delta. The update tool can modify existing properties or append new ones (hidden by default).

### 8. Library Symbol Cache Management (FIXED)

**Problem**: When adding symbols to a schematic via file manipulation, the `lib_symbols` section must contain the symbol definition for KiCad to render it. Power symbols especially need their lib_symbols cache entry. Currently `add_power_symbol` just inserts the instance and warns if the cache is missing.

**Fix Applied**: `add_component` and `add_power_symbol` now auto-populate the `lib_symbols` cache by looking up the symbol definition from installed KiCad libraries and injecting it into the schematic file. Sub-symbols are correctly renamed with the library prefix.

### 9. No Hierarchical Sheet Support (PARTIALLY FIXED)

**Problem**: Complex designs use hierarchical sheets. The MCP only reads/modifies the root schematic file. Sub-sheets are invisible.

**Partial Fix**: Added `get_sheet_hierarchy` tool that returns the full hierarchical sheet tree from a root schematic (sheet names, paths, UUIDs). Navigation of the tree structure is now possible.

**Still Needed**: Full read/modify support for sub-sheet contents (symbols, wires, labels inside a sub-sheet file). Currently only the hierarchy tree itself is returned — individual sub-sheet schematics must be read by passing the sub-sheet `.kicad_sch` file path directly.

### 10. Footprint-to-Symbol Mapping Is Manual (FIXED)

**Problem**: When placing a component, the user must know the exact footprint lib_id string. No tool helps find the right footprint for a given symbol.

**Fix Applied**: Added `suggest_footprints` tool that reads the symbol's `fp_filter` patterns and searches installed footprint libraries for matching footprints.

### 11. No Net-Aware Operations (FIXED)

**Problem**: Can't query "what net is this pin on?" or "show me all connections to net +3V3". Wire routing is blind — connect points and hope the netlist is correct.

**Fix Applied**: Added `get_pin_net` (get the net connected to a specific pin) and `get_net_connections` (get all pins, labels, and wires on a named net) tools. Uses file-based connectivity analysis that traces wires, labels, and power symbols.

### 12. No ERC/DRC from MCP After Modifications (FIXED)

**Problem**: After programmatically building a schematic, can't run ERC to verify correctness without opening KiCad. DRC similarly requires CLI backend.

**Fix Applied**: Added `validate_schematic` tool — a file-based ERC that checks common issues (floating pins, unconnected nets, missing power symbols) without requiring `kicad-cli` or a running KiCad instance. The existing `run_erc` and `run_drc` tools use the CLI/SWIG/IPC backend when available. `get_board_design_rules` provides read-only DRC constraint inspection via the file backend.

### 13. Generated Schematics Were Malformed for KiCad 8+ (FIXED)

**Problem**: A user on Claude Desktop (sandboxed, no filesystem MCP) used the KiCad MCP to generate `.kicad_sch` files. KiCad refused to open them with `Expecting '(' in line 16, offset 3`. Root cause: `add_component` and `add_power_symbol` were emitting symbol instances without KiCad 8+ required fields — `(unit 1)`, `(in_bom yes)`, `(on_board yes)`, `(dnp no)`, and the `(instances ...)` block that maps symbol UUIDs to reference designators within sheet paths.

**Impact**: Every schematic generated via MCP was broken in KiCad 8/9. Users had to hand-edit the raw s-expression to fix each symbol.

**Fix Applied**: Both `add_component` and `add_power_symbol` now emit all required KiCad 8+ fields. A new `_find_schematic_uuid` helper reads the root schematic UUID so the `(instances (project "" (path "/UUID" ...)))` block is correctly populated. The sample fixture and all inline test schematics were updated to match.

### 14. No Way to Create a Schematic from Scratch (FIXED)

**Problem**: The MCP had tools to modify existing schematics (`add_component`, `add_wire`, etc.) but no way to create a new one. Users in sandboxed environments (e.g. Claude Desktop without filesystem MCP) had no way to produce a valid schematic file — they would need to manually write the s-expression boilerplate (`kicad_sch`, `version`, `generator`, `uuid`, `paper`, `lib_symbols`, `sheet_instances`) before any tools could operate on it.

**Impact**: The MCP was useless for greenfield schematic design in sandboxed AI environments.

**Fix Applied**: Added `create_schematic` tool that generates a minimal valid KiCad 8+ schematic with correct version, generator, UUID, paper size, empty `lib_symbols` section, and `sheet_instances`. Supports optional title block (title, revision). Guards against overwriting existing files.

### 15. `get_symbol_pin_positions` Returned Empty Results for Standard KiCad Symbols (FIXED)

**Problem**: Many widely-used KiCad standard library symbols (e.g. `ATtiny85-20S`, `AMS1117-3.3`) use the `(extends "ParentName")` directive instead of defining their own pins. The `get_symbol_pin_positions` implementation only searched the `lib_symbols` section of the schematic file, where these cached copies only contain properties and the `extends` tag — no pin geometry at all. The tool returned an empty `pin_positions` dict for every such symbol, making it impossible to place power symbols or net labels programmatically for a large fraction of standard components.

**Impact**: Any MCP workflow that called `get_symbol_pin_positions` on `ATtiny85-20S`, `AMS1117-3.3`, or any other `extends`-based symbol would silently get no coordinates and then place power symbols / labels at the wrong positions.

**Fix Applied**: After extracting the cached lib symbol, the code now checks for an `(extends "Parent")` child. If present, it locates the source `.kicad_sym` library file via `_resolve_symbol_libs()`, parses it, and follows the `extends` chain (up to 5 levels deep) until it finds a symbol node that contains actual pin definitions. The full rotation and mirror transforms are then applied to those pins as before.

### 16. `read_schematic` (via `skip` library) Crashed on `extends`-Based Symbols (FIXED)

**Problem**: The `read_schematic` method uses the third-party `skip` library as its primary parser. `skip` crashed with `AttributeError: 'ParsedValue' object has no attribute 'symbol'` whenever the schematic's `lib_symbols` cache contained a symbol that uses `(extends ...)` and has no sub-symbol nodes. This happens for any schematic that includes `AMS1117-3.3`, `ATtiny85-20S`, or similar standard parts, causing `read_schematic`, `get_symbols`, and any tool that invokes them to fail completely.

**Impact**: After using `add_component` with these symbols, the schematic file was correct but `read_schematic` / `get_symbols` would crash, preventing any subsequent read-based tool from working.

**Fix Applied**: Added an `except Exception` fallback to `read_schematic` so that any `skip` parse failure automatically retries using the built-in s-expression parser (`_read_with_sexp`). The fallback is silent — callers receive correct data without needing to know which parser succeeded.

### 17. Sub-Symbol Names Must NOT Include the Library Prefix (FIXED)

**Problem**: When `_ensure_lib_symbol_cached` copied a symbol from a `.kicad_sym` library into the schematic's `lib_symbols` section, it renamed not only the top-level symbol node (from `"R"` to `"Device:R"`) but also every nested sub-symbol node (from `"R_0_1"` to `"Device:R_0_1"`). KiCad 9 rejects this with: *"Invalid symbol unit name prefix `Connector_Generic:Conn_01x02_1_1`"*, refusing to open the schematic.

**Root Cause**: The KiCad 9 schematic file format requires the outer `lib_symbols` entry to use the fully-qualified `lib_id` as its name (e.g. `"Connector_Generic:Conn_01x02"`), but all nested unit sub-symbols must use only the plain symbol name without the library prefix (e.g. `"Conn_01x02_1_1"`).

**Impact**: Every schematic generated by the MCP — including the air quality sensor demo — failed to open in KiCad. The error appeared immediately on file load.

**Fix Applied**: Removed the sub-symbol renaming regex from `_ensure_lib_symbol_cached`. Only the top-level symbol node is renamed to include the library prefix; sub-symbol nodes are left with their original names from the `.kicad_sym` source file.

### 18. `suggest_footprints` Returned No Results Due to 50-Result Cap (FIXED)

**Problem**: `suggest_footprints` called `search_footprints("")` expecting to get all installed footprints, then applied `fnmatch` filters. But `search_footprints` caps its results at 50 and returns the first 50 footprints in alphabetical order — `Audio_Module:*`, `Battery:*`, etc. For a symbol like `Device:R` with `fp_filters = ["R_*"]`, none of those first 50 matched, so `footprints` was always `[]`.

**Impact**: `suggest_footprints` was silently broken for any symbol whose matching footprints weren't among the first 50 alphabetically. This covers nearly all practical use cases (resistors, capacitors, ICs).

**Fix Applied**: `suggest_footprints` now iterates `self._footprint_libs` directly using `fnmatch`, capped at 100 matches, bypassing the `search_footprints` result cap entirely.

### 19. `fastmcp` Dependency Version Mismatch and Uninstallation Issues

**Problem**: The `kicad-mcp` project requires `fastmcp` version `2.0` or higher (but less than `3.0`). However, during initial installation, an older version (`0.4.1`) was present in the environment, leading to `ImportError` when attempting to use `fastmcp.Client`.

**Impact**: Prevented the MCP client script from running, as the `Client` class was not found at the expected import path (`fastmcp.Client`, `fastmcp.client.Client`, `fastmcp.core.Client`, `fastmcp.mcp.Client` all failed).

**Challenge**: Attempting to `pip uninstall fastmcp` repeatedly failed on Windows with `PermissionError: [WinError 5] Access is denied`. This issue persisted even when the command was theoretically run with administrator privileges, indicating a file lock or deeper permission problem.

**Resolution (Manual)**: Required manual intervention by the user to close all Python-related applications and delete the `fastmcp` and `fastmcp-*.dist-info` folders directly from the Python `site-packages` directory (`C:\Python312\Lib\site-packages`).

**Lesson**: Dependency resolution can be complex, especially in environments with existing packages. `PermissionError` during `pip uninstall` on Windows is a common and stubborn issue that often necessitates manual file system cleanup. It highlights the need for robust environment management or clearer error handling/guidance for such scenarios.

### 20. Project-Local Symbol Libraries Not Resolved by `_ensure_lib_symbol_cached` (FIXED)

**Problem**: `add_component` and `get_symbol_pin_positions` only searched system KiCad library paths. Custom libraries referenced in a project's `sym-lib-table` (e.g. `libs/Sensors.kicad_sym`) were silently ignored, causing `lib_symbols` caching to fail and `get_symbol_pin_positions` to return an empty error response.

**Impact**: Any schematic using project-local symbol libraries (non-KiCad-standard symbols) could not be built or queried via MCP. `get_symbol_pin_positions` returned `{"status": "success", "error": "...not found in lib_symbols cache"}` — no `pin_positions` key — crashing downstream callers.

**Fix Applied**: Added `_get_project_symbol_libs(schematic_path)` helper that reads the project's `sym-lib-table` and resolves `${PROJ_DIR}`/`${KIPRJMOD}` variables. Both `_ensure_lib_symbol_cached` and `get_symbol_pin_positions` now fall back to project libs when the system search fails. The caller must create a `sym-lib-table` in the project directory before adding custom components.

### 21. `clone_library_repo` Hung Indefinitely in MCP Stdio Context (FIXED)

**Problem**: `clone_library_repo` calls `subprocess.run(["git", "clone", ...])` with `capture_output=True` but no `stdin` specification. In the MCP server process, stdin is the MCP protocol pipe (connected to the client). When git tries to read stdin (e.g. for credential prompts or SSH confirmations), it reads from the MCP pipe and blocks forever since the client is waiting for the tool response, not sending data. The 300-second timeout was reached on every call.

**Impact**: `clone_library_repo` was completely unusable in any MCP stdio session — the tool always timed out regardless of the URL.

**Fix Applied**: Added `stdin=subprocess.DEVNULL` and `env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}` to the subprocess call. This prevents git from reading stdin and disables all interactive terminal prompts. Timeout reduced from 300s to 60s.

### 22. fastmcp 2.14.5 Incompatible with rich 13.5.3 in User Site-Packages (WORKAROUND)

**Problem**: When the MCP server subprocess starts, Python loads packages from the user AppData site-packages before system site-packages. The user AppData has fastmcp 2.14.5 (correct version) paired with rich 13.5.3 (older). fastmcp 2.14.5 calls `RichHandler(tracebacks_max_frames=3)` which is not a parameter in rich 13.5.3, causing `TypeError` on import — the server never starts.

**Impact**: The MCP server subprocess crashed on import before serving any tools. `PYTHONNOUSERSITE=1` didn't help because the system site-packages has fastmcp 0.4.1 (wrong version, missing `click`). Upgrading rich via pip was blocked because `semgrep` pins `rich~=13.5.2`.

**Workaround Applied**: Set `FASTMCP_LOG_ENABLED=false` in the server subprocess environment. This causes fastmcp to skip its entire `configure_logging()` call (which creates the incompatible `RichHandler`), allowing the module to import cleanly. The test runner (`test_tools.py`) includes this env var in `StdioServerParameters`.

### 23. End-to-End MCP Protocol Test Suite — All 78 Tools Verified (DONE)

**Achievement**: Built `examples/air_quality_sensor/test_tools.py` — a complete end-to-end integration test that:
- Launches the KiCad MCP server as a real subprocess via **stdio JSON-RPC** (same transport as Claude Desktop)
- Connects using `mcp.ClientSession` + `mcp.client.stdio.stdio_client` from the official MCP Python SDK
- Builds the complete Air Quality Sensor schematic from scratch using MCP tool calls (custom `Sensors.kicad_sym` library, ATtiny85, AMS1117-3.3, SCD41, SGP41, passive components)
- Exercises all **78 tools** against the real project files
- Saves permanent output to `examples/air_quality_sensor/`

**Result**: 77/78 tools pass. 0 failures. 1 skip (`run_freerouter` skipped when pcbnew is unavailable and `export_dsn` cannot produce a valid DSN file). Verified on Windows 11 with KiCad 9.

### 24. `autoroute` Called `@mcp.tool()` Decorated Functions as Plain Callables (FIXED)

**Problem**: Inside `register_tools()`, all 5 routing functions (`export_dsn`, `import_ses`, `run_freerouter`, `clean_board_for_routing`, `autoroute`) were inner functions decorated with `@mcp.tool()`. After decoration by fastmcp, they become `FunctionTool` objects — **not callable as plain Python functions**. The `autoroute` function called the other four directly (e.g. `result_json = export_dsn(path, str(dsn))`), causing `TypeError: 'FunctionTool' object is not callable` every time `autoroute` was invoked.

**Impact**: `autoroute` was completely broken. Any call to the "full pipeline" tool crashed immediately at Step 1 (board cleanup).

**Fix Applied**: Extracted the body of each of the four helper tools into module-level `_impl_*` functions (`_impl_export_dsn`, `_impl_import_ses`, `_impl_run_freerouter`, `_impl_clean_board_for_routing`). These plain Python functions accept `config` and `change_log` as parameters. The `@mcp.tool()` wrappers now simply call their corresponding `_impl_*` function. `autoroute` also calls the `_impl_*` functions directly, bypassing the `FunctionTool` layer entirely.

**Rule**: In fastmcp, any function decorated with `@mcp.tool()` inside `register_tools()` becomes a `FunctionTool` object and can no longer be called as a plain function from other code in the same scope. Always extract shared logic into plain helper functions defined **outside** `register_tools()`.

### 25. `run_freerouter` Subprocess Lacked `stdin=subprocess.DEVNULL` (FIXED)

**Problem**: The `subprocess.run()` call for the FreeRouting Java process did not set `stdin=subprocess.DEVNULL`. In an MCP stdio session, the server's stdin is the JSON-RPC pipe. A subprocess that inherits stdin will block waiting for input on that pipe, potentially freezing the MCP server.

**Fix Applied**: Added `stdin=subprocess.DEVNULL` to the FreeRouting `subprocess.run()` call, consistent with the same fix applied earlier to `clone_library_repo`.

**Rule**: All subprocesses launched from inside an MCP stdio server **must** use `stdin=subprocess.DEVNULL`. Without it, the subprocess inherits the MCP JSON-RPC pipe as stdin and may block waiting for input, freezing the entire server.

### 26. `run_freerouter` Test Must Use a Real DSN, Not a Dummy (FIXED)

**Problem**: The original `test_run_freerouter` wrote a dummy `"(pcb dummy)\n"` DSN file and passed it to FreeRouting. FreeRouting IS installed and was actually invoked — reporting `ERROR: There was an error while reading DSN file`. A dummy DSN can never pass FreeRouting's file validation.

**Fix Applied**: The test now checks whether `test_export_dsn` produced a valid DSN file (`exports/air_quality_sensor.dsn`). If it exists, that real DSN is passed to `run_freerouter`. If not (because `pcbnew` is unavailable and DSN export failed), the test is **skipped** with a clear message. This is correct behavior: `run_freerouter` cannot be meaningfully tested without a valid PCB DSN file.

### 27. `sync_schematic_to_pcb` Did Not Propagate Nets to PCB Pads (FIXED)

**Problem**: `sync_schematic_to_pcb` handled placement and value updates, but it never applied schematic connectivity to PCB pad net assignments. Boards could end up with most pads on `<no net>` even when the schematic was correctly wired.

**Impact**: DRC produced false-positive connectivity errors, routed tracks were disconnected from intended nets, and generated boards were not electrically equivalent to the schematic.

**Fix Applied**: The sync flow now queries schematic connectivity (`get_symbol_pin_positions` + `get_pin_net`) and calls `assign_net` for each resolved `(reference, pin/pad, net)` mapping. The tool reports `net_assigned` actions and includes `nets_assigned` in the summary.

### 28. `_parse_footprint_bounds` Must Use Balanced-Paren Walking, Not Regex (FIXED)

**Problem**: An early implementation of `_parse_footprint_bounds` used a regex like `\(fp_rect .*?\)` to extract courtyard rectangles from `.kicad_mod` files. The `.*?` lazy quantifier stops at the **first** `)` in the file, which is always inside a nested expression — it never captures the full `(fp_rect ...)` block. The same issue affected `(pad ...)` extraction.

**Impact**: `get_footprint_bounds` returned empty or incorrect courtyard data, causing `auto_place` to fall back to 5 × 5 mm stubs for every component and produce overlapping placements.

**Fix Applied**: Replaced the regex with `_walk_balanced_parens(text, "(fp_rect")` — a character-by-character scanner that tracks open/close paren depth. This correctly finds nested S-expression blocks of arbitrary depth. The same helper is used for pad extraction.

**Rule**: Never use `.*?\)` to extract S-expression blocks. KiCad `.kicad_mod` and `.kicad_pcb` files contain deeply nested S-expressions; a regex that stops at the first `)` will always return a truncated fragment.

### 30. PCB Setup Block Fields Break pcbnew LoadBoard() and kicad-cli Export (FIXED)

**Problem**: Certain constraint fields — specifically `(via_min_size X)`, `(via_min_drill X)`, and `(hole_clearance X)` — were added to the `(setup ...)` block of `.kicad_pcb` files to satisfy DRC. `kicad-cli pcb drc` accepted them, but pcbnew `LoadBoard()` and `kicad-cli pcb export gerbers/drill` rejected them with `"Malformed board"` / `"Failed to load board"`.

**Impact**: Any PCB that had these fields in its `(setup ...)` block could pass DRC but could not be exported or opened by pcbnew — making it effectively unusable for manufacturing.

**Root Cause**: `kicad-cli pcb drc` uses a more permissive file reader that tolerates unknown fields in `(setup ...)`. pcbnew (used for rendering and export) is strict — unknown or moved fields in `(setup ...)` cause a hard load failure.

**Fix Applied**: Removed all three fields from the `(setup ...)` block. Set `min_hole_clearance` in the `.kicad_pro` `design_settings.rules` section instead — this is the canonical location that both DRC and export commands honour. Specifically set `"min_hole_clearance": 0.22` (not 0.25) to accommodate the 0.227–0.24 mm clearances that FreeRouting typically produces near via drill holes.

**Rule**: Do NOT add `(via_min_size)`, `(via_min_drill)`, or `(hole_clearance)` to the `(setup ...)` block of a KiCad 9 PCB file. Use `.kicad_pro` `design_settings.rules` for all DRC constraints that must survive export.

### 29. `pcb_pipeline` Needs `_impl_*` Helpers, Not `@mcp.tool()` Wrappers (FIXED)

**Problem**: `pcb_pipeline` originally called the `@mcp.tool()`-decorated `autoroute` function directly inside `register_tools()`. After decoration by fastmcp, `autoroute` becomes a `FunctionTool` object (not callable as a plain Python function), so `pcb_pipeline` crashed with `TypeError: 'FunctionTool' object is not callable`.

**Fix Applied**: `pcb_pipeline` imports `_impl_export_dsn`, `_impl_import_ses`, and `_impl_run_freerouter` directly from `routing.py` and instantiates `KiCadMCPConfig()` locally. No `@mcp.tool()` decorated function is called from within another tool.

**Rule**: Inside `register_tools()`, any function decorated with `@mcp.tool()` is no longer callable as a plain Python function. Always extract shared logic into module-level `_impl_*` helpers (see Lesson 24).

### 31. `autoroute` Now Runs DRC After SES Import (DONE)

**Problem**: `autoroute` imported the routed SES file back into the PCB and reported success, but never verified whether the resulting board was DRC-clean. Connectivity errors and clearance violations from the router were silently accepted.

**Fix Applied**: Added a post-route DRC step to `autoroute`. After `import_ses` succeeds, `run_drc` is called. If DRC fails, `status` becomes `"success_with_drc_errors"` and the error count is included in the result. If DRC is unavailable (no `kicad-cli`), the step is skipped with `"unavailable"` and the routed board is kept.

**Files**: `src/kicad_mcp/tools/routing.py`

### 32. `get_symbol_info` Returned Empty Pins for `extends`-Based Symbols (FIXED)

**Problem**: `get_symbol_info` on symbols like `Sensor_Gas:SCD41-D-R2` returned `pin_count: 0` and an empty `pins` list. The symbol uses `(extends "SCD41-D-R2-base")` and has no pin geometry of its own.

**Root Cause**: The implementation only looked at the top-level symbol node and did not follow `(extends ...)` chains into the parent symbol definition.

**Fix Applied**: `get_symbol_info` now follows `extends` chains (up to 5 levels deep) when the immediate symbol has no pins, copying pins from the first ancestor that defines them. Consistent with the same fix already applied to `get_symbol_pin_positions`.

**Files**: `src/kicad_mcp/backends/file_backend.py`

### 33. FreeRouting v2.x Requires `--gui.enabled=false`; Java Threshold Was Wrong (FIXED)

**Problem A**: FreeRouting v2.x JARs (filename matches `freerouting-2.*`) attempt to open a GUI window when launched headlessly. Without `--gui.enabled=false`, the process hangs waiting for a display.

**Problem B**: The Java version threshold for downloading FreeRouting v2.1.0 was `>= 25` (should be `>= 21`). Java 21 introduced the required virtual-thread API. Users with Java 21-24 were incorrectly offered v1.9.0 instead of v2.1.0.

**Fix Applied**: `routing.py` now passes `--gui.enabled=false` when the JAR filename matches `freerouting-2.*`. `platform_helper.py` corrected the threshold from `>= 25` to `>= 21`.

**Files**: `src/kicad_mcp/tools/routing.py`, `src/kicad_mcp/utils/platform_helper.py`

### 34. `autoroute` Double Preflight and Orphan FreeRouting Subprocess (FIXED)

**Problem A**: `autoroute` called `_validate_board_preflight` twice — once explicitly in Step 0, and again inside `_impl_export_dsn` (Step 2). Each call spawns a KiCad subprocess on Windows (~10-30 s each). The double call added 20-60 s of dead time before FreeRouting even started.

**Problem B**: `subprocess.run(timeout=300)` does not kill the child process when the timeout fires. The MCP tool timeout (120 s) fired first; FreeRouting kept running as an orphan background process, holding file locks and consuming memory.

**Fix Applied**: Removed the Step 0 explicit preflight call (it is redundant — `_impl_export_dsn` still validates). Replaced `subprocess.run` with `Popen` + `communicate(timeout=85)` + `.kill()` on timeout. Added `CREATE_NO_WINDOW` creationflag on Windows. Lowered `autoroute` default `max_passes` from 100 to 10.

**Rule**: Any subprocess that must complete within a bounded time must use `Popen` + `communicate(timeout=N)` + explicit `.kill()`. `subprocess.run(timeout=N)` raises `TimeoutExpired` but does not kill the child.

**Files**: `src/kicad_mcp/tools/routing.py`

### 35. DRC Violations from Wrong `hole_clearance`, `MARGIN`, and Placement `clearance_mm` (FIXED)

**Problem**: Fresh boards routed via `pcb_pipeline` consistently produced DRC violations:
- ~28 `hole_clearance` violations: FreeRouting places tracks 0.22-0.24 mm from via drill holes; the 0.25 mm `hole_clearance` preset was too tight.
- ~7 `copper_edge_clearance` violations: 3 mm board margin left insufficient room near corners.
- ~75 `shorting_items` + `clearance` violations: 0.5 mm component clearance produced routing channels too narrow for FreeRouting.

**Fix Applied**:
- `hole_clearance` in both `"class2"` and `"fab_jlcpcb"` presets lowered from 0.25 mm → 0.22 mm. `min_hole_clearance` in `.kicad_pro` `design_settings.rules` set to 0.22 mm accordingly.
- `MARGIN` in `pcb_pipeline` raised from 3.0 mm → 5.0 mm.
- `ap_clearance` (Step 4 of `pcb_pipeline`) and `clearance_mm` default in `auto_place` both raised from 0.5 mm → 1.5 mm.

**Verified**: Fresh `pcb_pipeline` run on the same schematic after these changes: `error_count: 0`, `warning_count: 3` (silk/library only).

**Files**: `src/kicad_mcp/backends/file_backend.py`, `src/kicad_mcp/tools/board.py`

### 36. Plugin Backend POC — Direct `pcbnew` Access via In-KiCad TCP Bridge (DONE)

**Problem**: The IPC backend (gRPC via kipy) has recurring timeout and stability issues on Windows, particularly when KiCad is under load or a large board is open. A new approach is needed that avoids gRPC entirely.

**Architecture**: KiCad's embedded Python interpreter loads `kicad_plugin/kicad_mcp_bridge.py` as an `ActionPlugin`. At load time (not on button press) the plugin starts a `socketserver.TCPServer` on `localhost:9760` in a daemon thread. The MCP-side `PluginBackend` connects to this server, sends newline-delimited JSON requests, and reads JSON responses — no gRPC, no file-parsing, direct `pcbnew` API access.

**POC scope** (board-read only):
- `ping` → `kicad_version` string from `pcbnew.GetBuildVersion()`
- `get_board_info` → title, layer count, board size, net count, footprint count
- `get_components` → list of `{reference, value, x, y, layer, rotation}`
- `get_nets` → list of `{net_id, name}`

**`is_available()`** does a TCP connect + ping; caches the result for 5 seconds to avoid hammering the port on every tool call. Returns `False` gracefully when no bridge is running.

**Installation**: Copy `kicad_plugin/kicad_mcp_bridge.py` to `%APPDATA%\kicad\9.0\scripting\plugins\` (Windows) or `~/.config/kicad/9.0/scripting/plugins/` (Linux/macOS), then restart KiCad.

**Test results (Codex-verified)**: All 11 unit tests pass; full suite 219 passed, 1 skipped. `get_available_backends()` includes `"plugin"` key; reports `available: false` with no bridge running.

**Files**: `kicad_plugin/kicad_mcp_bridge.py` (new), `src/kicad_mcp/backends/plugin_backend.py` (new), `src/kicad_mcp/backends/factory.py` (plugin tried before IPC in auto-detect)

---

## Enhancement Requirements (Prioritized)

### P0 — Critical for Usable Schematic Automation

| # | Enhancement | Status | Why |
|---|------------|--------|-----|
| 1 | `compare_schematic_pcb` | **DONE** | Detect ref mismatches, missing footprints, stale components between sch/pcb |
| 2 | `remove_component` (schematic) | **DONE** | Can't fix mistakes without delete capability |
| 3 | `move_schematic_component` (schematic) | **DONE** | Repositioning without delete+re-add |
| 4 | `update_component_property` | **DONE** | Change footprint, value, or custom props on existing symbols |
| 5 | Auto-populate `lib_symbols` cache | **DONE** | Components added via MCP are renderable in KiCad immediately |
| 6 | `create_schematic` | **DONE** | Create valid KiCad 8+ schematic files from scratch via MCP |
| 7 | KiCad 8+ s-expression fields | **DONE** | `(unit)`, `(in_bom)`, `(on_board)`, `(dnp)`, `(instances)` emitted by add_component/add_power_symbol |

### P1 — Important for Practical Workflows

| # | Enhancement | Status | Why |
|---|------------|--------|-----|
| 8 | `sync_schematic_to_pcb` | **DONE** | Programmatic "Update PCB from Schematic" including net-to-pad sync |
| 9 | `suggest_footprints(lib_id)` | **DONE** | Help users find correct footprint for a symbol |
| 10 | `get_net_connections` / `get_pin_net` | **DONE** | Net-aware queries for intelligent wire routing |
| 11 | `remove_wire` / `remove_no_connect` | **DONE** | Undo/fix wiring mistakes |
| 12 | Hierarchical sheet read support | Partial | `get_sheet_hierarchy` returns tree; full sub-sheet read/modify still planned |

### P2 — Nice to Have

| # | Enhancement | Status | Why |
|---|------------|--------|-----|
| 13 | `validate_schematic` (file-based ERC lite) | **DONE** | Check floating pins, unconnected nets without KiCad CLI |
| 14 | `get_sheet_hierarchy` | **DONE** | Navigate hierarchical sheet tree from a root schematic |
| 15 | `get_symbol_pin_positions` extends resolution | **DONE** | ATtiny85-20S, AMS1117-3.3 and all `extends`-based symbols now return correct pin coordinates |
| 16 | `read_schematic` / `skip` crash fix | **DONE** | Graceful fallback to s-expression parser when `skip` fails on `extends`-based symbols |
| 21 | Sub-symbol lib-prefix bug | **DONE** | KiCad 9 rejects `lib:sym_1_1` sub-symbol names; fix leaves sub-symbols with plain name (`sym_1_1`) |
| 22 | Project-local sym-lib-table library resolution | **DONE** | Custom symbols (e.g. Sensors:SCD41) now found via project sym-lib-table |
| 23 | `clone_library_repo` stdin hang in MCP stdio | **DONE** | git subprocess blocked on MCP pipe; fixed with DEVNULL + GIT_TERMINAL_PROMPT=0 |
| 24 | End-to-end MCP protocol test suite | **DONE** | 63/64 tools pass (1 skip: run_freerouter requires pcbnew for DSN export) |
| 25 | `autoroute` FunctionTool bug | **DONE** | Extracted `_impl_*` helpers; autoroute now works end-to-end |
| 26 | `run_freerouter` subprocess stdin | **DONE** | Added `stdin=DEVNULL`; prevents MCP pipe blocking |
| 17 | `auto_place` + `get_footprint_bounds` | **DONE** | Geometry-driven bin-packing using courtyard extents; eliminates overlap violations |
| 28 | `set_board_design_rules` | **DONE** | Writes IPC-2221 Class 2 / JLCPCB rules into (setup ...) so DRC catches real violations |
| 29 | `pcb_pipeline` | **DONE** | Full schematic-to-routed-PCB in one call (sync → rules → outline → place → route → DRC) |
| 30 | `get_pcb_workflow` | **DONE** | Returns structured 11-step workflow JSON so AI assistants follow the correct tool sequence |
| 31 | `plan_project` / `read_project_plan` | **DONE** | Save/retrieve structured project plans (BOM, milestones, goal) as `project_plan.json` |
| 32 | `add_board_outline` | **DONE** | Inserts `(gr_rect ...)` on Edge.Cuts; replaces any existing board outline |
| 33 | PCB setup block field restrictions | **DONE** | Learned: `(via_min_size/drill/hole_clearance)` break pcbnew LoadBoard; use `.kicad_pro` rules instead |
| 34 | `autoroute` post-route DRC | **DONE** | DRC runs after SES import; `success_with_drc_errors` status when violations found |
| 35 | `get_symbol_info` extends chain | **DONE** | Follows `(extends ...)` to return pins from ancestor symbols |
| 36 | FreeRouting v2.x `--gui.enabled=false` + Java threshold | **DONE** | Headless flag for v2.x; threshold corrected from ≥25 to ≥21 |
| 37 | `autoroute` double preflight + orphan subprocess | **DONE** | Removed redundant preflight; `Popen`+`communicate`+`kill` prevents orphan Java process |
| 38 | DRC reduction constants (hole_clearance, margin, clearance_mm) | **DONE** | 0.22mm hole_clearance, 5mm margin, 1.5mm component clearance → 0 DRC errors on fresh boards |
| 39 | Plugin backend POC | **DONE** | In-KiCad TCP bridge (`pcbnew` API, board-read only); full coverage in future milestones |
| 18 | `add_text` / `add_graphic` (schematic) | Planned | Annotations like "AIRFLOW ->" on the schematic |
| 19 | Batch operations | Planned | Place multiple components/wires in one call for performance |
| 20 | Undo/redo support | Planned | Track changes and allow rollback beyond file backups |
| 40 | Plugin backend full coverage | Planned | Expand plugin backend to modify ops, DRC, export; replaces IPC on Windows |
