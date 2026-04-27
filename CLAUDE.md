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

---

## PCB Design Protocol — Mandatory Step Order

Use `/build-pcb [description]` to start a phased PCB design session. The skill
enforces the 7-phase professional workflow below, with a report and user confirmation
between each phase.

For reference, the phases and their gate conditions are:

| Phase | Name | Gate condition |
|-------|------|---------------|
| 1 | Environment & Requirements | `get_startup_checklist.ready_for_pcb == true` |
| 2 | Schematic Capture | All components placed, ≥1 net |
| 3 | Schematic Verification | `validate_schematic_for_pcb.ready_for_pcb_sync == true`, ERC clean |
| 4 | PCB Setup & Placement | `check_courtyard_overlaps.passed == true` |
| 5 | Routing | Zero unrouted connections |
| 6 | Design Verification | `run_drc.passed == true` (or kicad-cli unavailable — document) |
| 7 | Manufacturing Outputs | All six export files generated |

### Rules (hard constraints — no exceptions)
- **NEVER** call `sync_schematic_to_pcb` when `validate_schematic_for_pcb` returns blocking issues
- **NEVER** call `autoroute` when `check_courtyard_overlaps` returns failures
- **NEVER** skip `get_startup_checklist` at the start of any session involving PCB ops
- Use `clear_routes` + `auto_place` instead of routing over bad placement
- Board size MUST come from `estimate_board_size`, not intuition or guesswork
- After any `move_component` batch, call `diff_board` to confirm only expected footprints moved
