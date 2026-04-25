# KiCad MCP — Development Guidelines

## MCP Tool Usage — CRITICAL RULE

**Never write Python scripts or shell scripts to operate the KiCad MCP.**

The KiCad MCP server runs automatically and its tools (`mcp__kicad__*`) are
available directly in every Claude Code session. Always call them directly:

- `mcp__kicad__create_schematic` — not a Python script that calls `FileSchematicOps`
- `mcp__kicad__add_component` — not `backend.add_symbol(...)`
- `mcp__kicad__place_component` — not `FileBoardOps().place_component(...)`
- `mcp__kicad__autoroute` — not `subprocess.run(["java", "-jar", "freerouting.jar"])`

If an MCP tool is broken, **fix the tool** — do not work around it with a script.
Scripts that bypass the MCP are forbidden regardless of whether they "work".

## Backend Code Changes

When modifying `src/kicad_mcp/`, run the test suite to verify nothing regressed:

```
source .venv/bin/activate && python -m pytest --tb=short -q
```

## Footprint Fixture Libraries

Real `.kicad_mod` files for testing live in `tests/fixtures/footprints/`.
Tests patch `find_footprint_libraries` to point there — no system KiCad required.
