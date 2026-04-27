# KiCad MCP — Development Roadmap

*Branch: `feat/plugin-backend` · Last updated: 2026-04-14 (phased /build-pcb workflow + pcb_pipeline courtyard gate)*

---

## Guiding Principles

- **Plugin bridge is primary.** The subprocess/file fallback exists only for environments where KiCad isn't running. Don't let it become the happy path.
- **Fix tools, never work around them.** If a tool is broken, fix the tool. Scripts that bypass MCP are forbidden.
- **No speculative abstractions.** Add features when there's a concrete use-case, not "just in case".
- **Test at the boundary.** Unit tests for pure logic; E2E tests via the MCP protocol for tool behavior.

---

## Phase 1 — Stability (current sprint)

These are known bugs or fragile behaviors that affect existing workflows.

### 1.1 Fix bridge reinstall — wx bare-name bug
**Status**: Source fixed (2026-04-07). Bridge code uses `_save_and_refresh()` with `try: import wx`. **Still needs**: run `kicad_plugin/install_bridge.ps1` and restart PCB editor to push the fix into KiCad's scripting/plugins directory.

**Impact**: `export_gerbers` and `export_drill` fail when the installed bridge has the old `save_board` handler that references `wx` as a bare name. This blocks the full export workflow.

**Owner**: Codex (requires live KiCad session).

---

### 1.2 Fix open_kicad board-switch — async race
**Status**: Unresolved. Calling `open_kicad(path=B)` when board A is open launches pcbnew with B, but the bridge may still be watching A. All subsequent bridge ops fail with "does not match open board".

**Root cause**: Board switch is asynchronous; there is no wait-and-verify mechanism. The bridge's `_get_open_board()` checks the path before pcbnew has finished loading the new file.

**Fix plan**:
1. After `open_kicad`, poll `get_active_project()` until `board_path` matches or timeout (5 s, 500 ms intervals).
2. Add a `wait_for_board(path, timeout=5.0)` helper in `PluginDirectBackend` (not an MCP tool).
3. Call it from `open_kicad` when the bridge is active.

**Scope**: `src/kicad_mcp_plugin/backends/plugin_direct.py` + `src/kicad_mcp/tools/project.py` (`open_kicad` handler).

---

### 1.3 Add pytest test suite
**Status**: Done (2026-04-12). `tests/` directory created with four focused test modules covering the Phase 2 workflow tools:

| Test module | Covers |
|---|---|
| `tests/test_clear_routes.py` | `FileBoardOps.clear_routes()` — segment/via removal, footprint/net preservation, backup |
| `tests/test_check_courtyard_overlaps.py` | `check_courtyard_overlaps` tool — AABB detection, refs, dimensions, empty board |
| `tests/test_estimate_board_size.py` | `estimate_board_size` tool — area math, `_ceil5` rounding, margin inflation |
| `tests/test_startup_checklist.py` | `run_startup_checklist()` — all 6 checklist items, PASS/FAIL/WARN logic, ready_for_pcb gate |

All tests run without KiCad installed (mock `_tcp_call`, `is_kicad_running`, `_load_kicad_mod`).

---

### 1.4 Merge `feat/plugin-backend` → `main`
After 1.1–1.3 are done, this branch is release-ready. Merge checklist:
- [ ] Bridge reinstalled and gerber/drill export confirmed working
- [ ] Board-switch fix passing (Codex E2E)
- [x] pytest suite green with `--tb=short -q`
- [x] README reflects plugin-first architecture and install steps
- [x] Tool count updated in docs (83 tools)

---

## Phase 2 — Workflow Improvements (Completed 2026-04-12)

Five new MCP tools and targeted enhancements to reduce manual steps in the PCB design workflow.

### 2.1 `get_startup_checklist` — pre-flight environment check
Runs 6 ordered checks (`kicad_running`, `bridge_reachable`, `bridge_version_ok`, `pcb_editor_open`, `kicad_cli_available`, `project_loaded`) and returns `{"ready_for_pcb": bool, "checklist": [...], "required_actions": [...]}`. `kicad_cli_available` is WARN (not FAIL) so missing kicad-cli doesn't block routing workflows. `pcb_pipeline` calls the underlying `run_startup_checklist()` function as Step 0a.

### 2.2 `estimate_board_size` — automatic PCB dimension estimation
Given a list of footprint IDs, sums courtyard areas, adds a routing overhead percentage (default 20%), computes a landscape bounding box, adds edge clearance, rounds up to the nearest 5 mm, and applies a margin. `plan_project` uses this when `board_width_mm` / `board_height_mm` are left at 0 (new default). Returns per-component breakdown and a list of missing footprints.

### 2.3 `validate_schematic_for_pcb` — schematic readiness gate
File-based checks that catch common issues before sync: missing footprints, duplicate or blank reference designators, no PWR_FLAG on VCC/GND nets, components at (0, 0), zero net count. Optionally calls `kicad-cli sch erc`. `pcb_pipeline` calls `run_validate_schematic_for_pcb()` as Step 0b.

### 2.4 `check_courtyard_overlaps` — AABB overlap detection
Parses all `(footprint ...)` blocks from the .kicad_pcb file, applies `(at x y rotation)` transforms, and runs O(n²) AABB intersection across all courtyard rectangles. Reports each pair with `overlap_x_mm`, `overlap_y_mm`, and a `suggested_move_mm` to resolve the conflict.

### 2.5 `clear_routes` — non-destructive rip-up
Strips all `(segment ...)` and `(via ...)` blocks from the .kicad_pcb file while preserving footprint placement, nets, and the board outline. Writes a `.clear_routes_backup.kicad_pcb` before modifying. When the plugin bridge is active, reloads the board in KiCad automatically.

### 2.6 Tool and pipeline enhancements
- **`suggest_footprints`**: each suggestion now includes `width_mm`, `height_mm`, `area_mm2` from the courtyard bounds.
- **`plan_project`**: `board_width_mm` / `board_height_mm` default to 0 (auto-estimate); emits `size_warning` if explicit dimensions are >15% smaller than the estimate.
- **`auto_place`**: computes `utilization_pct` after placement and warns if >70%.
- **`pcb_pipeline`**: Step 0 pre-flight gate (startup check + schematic validation + size check) aborts the pipeline early if the environment isn't ready. Board outline is now centered at the KiCad canvas origin (0, 0) so the board always appears in the middle of the work area. Step 4b courtyard gate added — pipeline now fails with a clear message if `check_courtyard_overlaps` finds collisions after auto_place, enforcing the CLAUDE.md hard rule against routing over overlaps.
- **`check_courtyard_overlaps`**: core logic extracted to `run_check_courtyard_overlaps()` module-level function so both the MCP tool and `pcb_pipeline` share one implementation.
- **`export_pdf`**: surfaces actionable error when kicad-cli is missing; catches zero-exit / no-file-produced failures and returns stderr.
- **`CLAUDE.md`**: PCB Design Protocol replaced with 7-phase professional table + hard rules; references `/build-pcb` Claude Code skill.
- **`/build-pcb` Claude Code skill** (`.claude/commands/build-pcb.md`): invoked as `/build-pcb [description]`; uses `EnterPlanMode` for upfront phase sign-off, then executes all 7 phases with a structured report and user confirmation pause between each. Phases mirror IPC/JEDEC industry practice: Environment & Requirements → Schematic Capture → Schematic Verification → PCB Setup & Placement → Routing → Design Verification → Manufacturing Outputs.

---

## Phase 3 — Architecture (next sprint)

Structural improvements that make the codebase easier to maintain and extend.

### 3.1 Retire `kicad_mcp.__main__` legacy entry point
**Context**: `.mcp.json` now points exclusively to `kicad_mcp_plugin`. The legacy `kicad_mcp.__main__` entry point (file/CLI/IPC backend chain) is no longer referenced in any config or documentation. It's a maintenance burden — every tool change must be compatible with both paths.

**Plan**:
1. Keep the `kicad_mcp` package (it's imported by `kicad_mcp_plugin`).
2. Delete `src/kicad_mcp/__main__.py` and the `kicad_mcp` console script entry point in `pyproject.toml`.
3. Update README to remove references to `python -m kicad_mcp`.
4. Remove `CompositeBackend.check_file_write_safe()` and the `_check_file_write_safety` guard (no longer needed; plugin bridge is always the board writer when KiCad is running).

**Risk**: Low. The legacy path has no known active users.

---

### 3.2 `BackendProtocol` → concrete base class
**Context**: `BackendProtocol` is an abstract class that both `CompositeBackend` and `PluginDirectBackend` implement independently. They share significant surface area (all tool entry points call the same method names). Any new method added to one must be manually mirrored in the other.

**Plan**:
1. Promote `BackendProtocol` from ABC to a concrete base class with default implementations that raise `NotImplementedError`.
2. `PluginDirectBackend` and `CompositeBackend` inherit from it and override only what they need.
3. New methods added to the base automatically become available without requiring changes in both subclasses.

**Scope**: `src/kicad_mcp/backends/base.py`, `plugin_direct.py`, `composite.py`.

---

### 3.3 Bridge watchdog — reconnect after KiCad restart
**Context**: If KiCad crashes or is restarted, `PluginDirectBackend._bridge_available` stays `True` from the startup probe, but all subsequent TCP calls fail with `ConnectionRefusedError`. The user must restart the MCP server to recover.

**Plan**:
1. In `_tcp_call`, catch `ConnectionRefusedError` / `OSError` and raise a `BridgeTemporarilyUnavailableError` (distinct from `BridgeNotAvailableError`).
2. In `PluginDirectBackend`, catch this error, reset `_bridge_available = False`, and return a helpful error message to the tool caller (don't crash the server).
3. Subsequent calls will re-probe on the next `is_available()` check.

**Scope**: `kicad_plugin/kicad_mcp_bridge.py` (no change), `plugin_direct.py`, `plugin_backend.py`.

---

### 3.4 `PluginDirectBackend` — `reload_board` is a no-op after `import_ses`
**Context**: After FreeRouting writes to disk and the bridge's `import_ses` handler imports the SES, the bridge calls `_save_and_refresh()`. KiCad's `Refresh()` updates the GUI but subsequent bridge reads may not reflect the new tracks until the next `LoadBoard`. The `reload_board` handler currently calls `Refresh()` only, not `LoadBoard`.

**Plan**:
1. In `kicad_mcp_bridge.py`, modify `_handle_reload_board` to attempt `board.Load(path)` inside a try/except, then call `Refresh()`.
2. Test with `get_tracks` before/after `import_ses` to confirm new tracks appear.
3. Update bridge via `install_bridge.ps1`.

---

## Phase 4 — Features (medium-term)

New capabilities that address real workflow gaps.

### 4.1 `auto_place` — use pcbnew native bounding boxes when bridge is active
**Context**: The current `auto_place` tool always uses file-based courtyard parsing (`_parse_footprint_bounds`). When the bridge is active, pcbnew has accurate footprint bounding boxes (including copper, silkscreen) via `GetBoundingBox()`. Native boxes would produce tighter, more accurate placement.

**Plan**:
1. Add `auto_place_native` handler in the bridge that calls `GetBoundingBox()` per footprint and then calls `MoveComponent()`.
2. Add `auto_place` to `PluginBoardOps` (TCP call) and expose it via `PluginDirectBackend`.
3. The `auto_place` MCP tool tries `backend.get_board_modify_ops().auto_place()` first (existing plugin path), falls back to `FileBoardOps().auto_place()` on `NotImplementedError`.

**Note**: The bridge's `auto_place` handler needs to be implemented in `kicad_mcp_bridge.py` and installed.

---

### 4.2 `get_board_3d` — export 3D board model via kicad-cli
**Context**: kicad-cli supports `kicad-cli pcb export step` and `kicad-cli pcb export vrml`. These produce STEP and VRML files useful for mechanical integration. No MCP tool exposes this yet.

**Plan**:
1. Add `export_step(board_path, output)` and `export_vrml(board_path, output)` to `CLIBoardOps` (or a new `CLIExportOps` method).
2. Register as `mcp__kicad__export_step` and `mcp__kicad__export_vrml` tools.
3. Guard with `save_board()` before running kicad-cli (same pattern as `export_gerbers`).

---

### 4.3 `validate_board` — file-based pre-flight checks without kicad-cli
**Context**: `validate_schematic` exists for schematics. There's no equivalent for PCB files. Users can't check "does the board have an Edge.Cuts outline?", "are all footprints placed?", "are there duplicate reference designators?" without calling `run_drc` (which requires kicad-cli).

**Plan**:
1. Add `validate_board(path)` tool backed by `FileBoardOps`.
2. Checks: Edge.Cuts outline present + closed, no duplicate reference designators, all footprints have at least one pad, no footprints at (0, 0), design rules block present in `.kicad_pro`.
3. Returns `{"passed": bool, "issues": [...]}` — same shape as `run_drc`.

---

### 4.4 `place_component` bulk API
**Context**: `pcb_pipeline` calls `place_component` once per footprint in a loop. Each call does a full file read/write cycle. For a 30-component board this is 30 round trips, adding ~2 s overhead.

**Plan**:
1. Add `place_components_bulk(path, components: list[dict])` to `FileBoardOps` and `PluginBoardOps`.
2. `FileBoardOps` implementation reads the file once, appends all footprint blocks, writes once.
3. Bridge implementation batches all `place_component` calls in one wx main thread dispatch.
4. Use in `pcb_pipeline`'s sync step.

---

### 4.5 Schematic ERC — net-connectivity aware checks
**Context**: The current `validate_schematic` tool does syntactic checks (pin types, unconnected wires). It doesn't trace net connectivity — it can't detect "pin A and pin B are in the same net but have conflicting power directions" the way a real ERC does.

**Plan**: Extend `validate_schematic` to use `_build_connectivity()` for power conflict detection (multiple `pwr_output` pins on the same net with no PWR_FLAG). This covers the most common ERC error without requiring kicad-cli.

---

### 4.6 `diff_board` — detect changes between two board snapshots
**Context**: After `pcb_pipeline` runs, users sometimes want to know what changed vs. the previous state. Currently they must manually compare files.

**Plan**:
1. `diff_board(path_a, path_b)` → reads both, compares component positions, track counts, net assignments.
2. Returns `{"added_components": [...], "removed_components": [...], "moved_components": [...], "track_delta": n}`.
3. Useful for: confirming `autoroute` added tracks, verifying `auto_place` moved all components.

---

## Phase 5 — Production Readiness (long-term)

### 5.1 Packaging — single-file bridge installer via pip
**Status**: Done (2026-04-12). `kicad-mcp install-bridge` CLI registered in `pyproject.toml`.
Bridge source bundled as package data via `hatch force-include`. Supports `--kicad-version`
and `--dry-run`. Works on Windows, macOS, and Linux.

### 5.2 CI/CD — GitHub Actions
**Status**: Done (2026-04-12). `.github/workflows/ci.yml` added.
- `pytest` runs on Python 3.10/3.11/3.12, blocks on failure.
- `mypy` runs informational (non-blocking, `continue-on-error: true`) until annotation
  coverage improves.
- PyPI publish triggers on `refs/tags/v*` push after tests pass.

### 5.3 MCP tool count audit
Currently 83 tools (78 at Phase 1 + 5 added in Phase 2). After Phase 4 additions:
- `export_step`, `export_vrml` → +2
- `validate_board` → +1
- `diff_board` → +1
- `place_components_bulk` → +1 (or internal only)

Target: ~88 tools, all tested by the E2E suite.

### 5.4 Linux/macOS bridge support
**Status**: Done (2026-04-12).
- `_get_kicad_python()` now probes snap prefix (`/snap/kicad/current/usr/bin/python3`)
  before falling back to `/usr/bin/python3` and `/usr/local/bin/python3`.
- `_run_pcbnew_script()` now sets `PYTHONPATH` to KiCad's dist-packages on Linux and
  macOS so system-package pcbnew installs are importable in the subprocess.

---

## Quick Reference: Known Technical Debt

| Item | File | Severity |
|---|---|---|
| Bridge wx bare-name bug (needs reinstall) | `kicad_mcp_bridge.py` (installed copy) | High |
| `open_kicad` board-switch async | `tools/project.py` | High |
| `kicad_mcp.__main__` legacy entry point | `src/kicad_mcp/__main__.py` | Medium |
| `reload_board` uses `Refresh()` not `LoadBoard` | `kicad_mcp_bridge.py` | Medium |
| `auto_place` uses file-based bounds (not pcbnew native) | `tools/board.py`, `file_backend.py` | Low |
| Linux pcbnew path detection incomplete | `backends/subprocess_backend.py` | Low |
