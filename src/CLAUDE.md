# KiCad MCP — Development Guidelines

> These rules apply when modifying source code in `src/kicad_mcp/` or `src/kicad_mcp_plugin/`.
> The root `CLAUDE.md` (MCP usage rules + PCB protocol) is always in effect alongside these.

## After Any Code Change

Run the full test suite before committing:

```
.venv\Scripts\activate && python -m pytest --tb=short -q
```

All 137 tests must pass. Tests mock `_tcp_call`, `is_kicad_running`, and `_load_kicad_mod` —
no KiCad installation required.

## Codebase Layout

```
src/kicad_mcp/              ← shared package (both entry points import from here)
  backends/                 ← composite, plugin_backend, cli, file, ipc, swig
  tools/                    ← board, schematic, export, routing, library, drc, project
  utils/                    ← platform_helper, sexp_parser, change_log, validation

src/kicad_mcp_plugin/       ← plugin entry point (primary, used by .mcp.json)
  backends/plugin_direct.py ← PluginDirectBackend — fixed routing, no fallbacks
  server.py                 ← registers open_kicad, bridge guard, all tools
```

## Two Entry Points

| Entry point | Command | Board ops |
|---|---|---|
| **Plugin** (primary) | `python -m kicad_mcp_plugin` | TCP bridge → pcbnew |
| **Legacy** | `python -m kicad_mcp` | File backend (read-only for boards) |

`.mcp.json` always points to `kicad_mcp_plugin`. The legacy entry point is slated for removal (Phase 3.1).

## Adding a New Tool

1. Add the implementation to the appropriate module in `src/kicad_mcp/tools/`
2. Register it in `register_tools(mcp, backend, change_log)` — follow existing patterns
3. If it needs live pcbnew data, add a handler to the bridge dispatch table (`kicad_plugin/kicad_mcp_bridge.py`) and call it via `_tcp_call`
4. Write a pytest test — mock at the TCP/file boundary, not inside the tool logic

## Footprint Test Fixtures

Real `.kicad_mod` files for testing live in `tests/fixtures/footprints/`.
Tests patch `find_footprint_libraries` to point there — add new fixtures here when
testing footprint-related code. No system KiCad installation needed.

## What Not to Do

- Do not add `CompositeBackend` fallbacks that hide bridge failures — use `BridgeNotAvailableError`
- Do not write scripts that call `FileSchematicOps`, `FileBoardOps`, etc. directly — fix the MCP tool
