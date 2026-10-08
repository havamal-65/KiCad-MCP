# KiCad MCP — Development Roadmap

*Last updated: 2026-10-06 (Phase 6 marked shipped; technical-debt table refreshed)*

---

## Guiding Principles

- **Plugin bridge is primary.** The subprocess/file fallback exists only for environments where KiCad isn't running. Don't let it become the happy path.
- **Fix tools, never work around them.** If a tool is broken, fix the tool. Scripts that bypass MCP are forbidden.
- **No speculative abstractions.** Add features when there's a concrete use-case, not "just in case".
- **Test at the boundary.** Unit tests for pure logic; E2E tests via the MCP protocol for tool behavior.

---

## Phase 1 — Stability (**Complete**)

These are known bugs or fragile behaviors that affect existing workflows. All resolved as of 2026-05-01.

### 1.1 Fix bridge reinstall — wx bare-name bug + KiCad 9 API renames
**Status**: Done (2026-05-01). `install_bridge.ps1` run; fixed bridge pushed to KiCad plugin directory. **Requires**: close and reopen PCB editor once to activate.

- `_save_and_refresh()` wx import fix: 2026-04-07
- `SetDrillValue` → `SetDrill` KiCad 9 API rename: `0eb29e4` (2026-04-28)
- Bridge reinstalled: 2026-05-01

**Impact**: `export_gerbers`, `export_drill`, and `add_via` drill size were broken on the old installed bridge. Both now fixed.

---

### 1.2 Fix open_kicad board-switch — async race
**Status**: Done. `_wait_for_board()` polling helper added to `server.py` during `feat/plugin-backend` work. After `launch_pcbnew`, `open_kicad` polls `get_active_project()` on 500 ms intervals (10 s timeout) until `board_path` matches the requested board. Returns `"bridge": "pending"` (retry signal) if board hasn't loaded within the timeout. Roadmap entry was stale — fix was already in the merged code.

---

### 1.3 Add pytest test suite
**Status**: Done (2026-04-12). `tests/` directory with four focused test modules:

| Test module | Covers |
|---|---|
| `tests/test_clear_routes.py` | `FileBoardOps.clear_routes()` — segment/via removal, footprint/net preservation, backup |
| `tests/test_check_courtyard_overlaps.py` | `check_courtyard_overlaps` tool — AABB detection, refs, dimensions, empty board |
| `tests/test_estimate_board_size.py` | `estimate_board_size` tool — area math, `_ceil5` rounding, margin inflation |
| `tests/test_startup_checklist.py` | `run_startup_checklist()` — all 6 checklist items, PASS/FAIL/WARN logic, ready_for_pcb gate |

All tests run without KiCad installed (mock `_tcp_call`, `is_kicad_running`, `_load_kicad_mod`).

---

### 1.4 Merge `feat/plugin-backend` → `main`
**Status**: Complete (2026-04-27). `feat/plugin-backend` branch deleted; only `main` remains.
- [x] Bridge reinstalled and gerber/drill export confirmed working (1.1 above)
- [x] Board-switch fix confirmed in code (1.2 above)
- [x] pytest suite green with `--tb=short -q` (137 tests)
- [x] README reflects plugin-first architecture and install steps
- [x] Tool count updated in docs (94 tools as of 2026-04-28)

---

## Phase 2 — Workflow Improvements (**Complete** 2026-04-12)

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
Removes all tracks and vias while preserving footprint placement, nets, and the board outline. Writes a `.clear_routes_backup.kicad_pcb` before modifying.

Initial implementation (2026-04-12) was file-only and called `reload_board` to refresh the bridge's view. Reworked 2026-05-07 to dispatch through the bridge when active: `_handle_clear_routes` snapshots `board.GetTracks()`, calls `board.Remove()` per item, then `_save_and_refresh()` so file and in-memory cache mutate together. Falls back to the original file-walking path when the bridge is unreachable. Eliminates the "track count compounds across iterations" desync that occurred when the bridge cache fell out of sync with disk after a file-only clear.

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

## Phase 3 — Architecture (**Complete** 2026-05-02)

Structural improvements that make the codebase easier to maintain and extend.

### 3.1 Retire `kicad_mcp.__main__` legacy entry point
**Status**: Done (2026-05-02).

- `src/kicad_mcp/__main__.py` deleted; `kicad-mcp` console script repurposed to bridge installer.
- README "Programmatic Configuration" updated to `kicad_mcp_plugin.server.create_plugin_server`.
- `src/CLAUDE.md` "Two Entry Points" table removed; sole entry point documented.
- `CompositeBackend.check_file_write_safe()` / `_check_file_write_safety` guard: already absent.

---

### 3.2 `BackendProtocol` → concrete base class
**Status**: Done (pre-existing). `BackendProtocol` in `src/kicad_mcp/backends/base.py:445` is already a concrete class with `NotImplementedError` default implementations. `PluginDirectBackend` inherits from it at `plugin_direct.py:53` and overrides all methods it needs. Roadmap entry was stale — implementation was already in place before this sprint.

---

### 3.3 Bridge watchdog — reconnect after KiCad restart
**Status**: Done (pre-existing). Full watchdog chain already implemented:

1. `_tcp_call` (`plugin_backend.py:75`) catches `ConnectionRefusedError`/`OSError` → raises `BridgeTemporarilyUnavailableError`
2. `PluginBoardOps._call()` catches it → invokes `_on_disconnect()` callback
3. `PluginDirectBackend._on_bridge_disconnect()` (`plugin_direct.py:95`) resets `_bridge_available = False`
4. Subsequent `is_available()` check re-probes and sets `_bridge_available = True` on success

Roadmap entry was stale — implementation was already in place before this sprint.

---

### 3.4 `PluginDirectBackend` — `reload_board` is a no-op after `import_ses`
**Status**: Partially done. `_handle_reload_board` in `kicad_mcp_bridge.py:734` calls `board.Load(filename)` with a fallback to `wx.CallAfter(pcbnew.Refresh)` if `Load` fails.

Caveat (discovered 2026-05-06): on KiCad 9 the `board.Load(filename)` call raises in the bridge's embedded Python and the handler silently falls through to the GUI-only `Refresh()`, which does not rebuild pcbnew's in-memory track cache. After `import_ses` this is harmless because the bridge handler already mutated the in-memory board directly (`pcbnew.ImportSpecctraSES(board, ...)` then `_save_and_refresh`), so the post-import `reload_board` call is effectively redundant. The same fallback was the source of the `clear_routes` desync — fixed for that path in 2.5 (2026-05-07) by routing the operation through the bridge handler instead of relying on reload. Generic post-disk-write reload remains an open issue for any future code path that relies on it; the working convention is "always mutate the live board, never write-then-reload."

---

### 3.5 Wire `parts` catalog into plugin server
**Status**: Done (2026-05-02).

- `src/kicad_mcp/tools/parts.py:25` — import changed from `CompositeBackend` to `BackendProtocol`; annotation at line 41 changed to `backend: BackendProtocol`.
- `src/kicad_mcp_plugin/server.py:16` — `parts` added to tools import; `parts.register_tools(mcp, backend, change_log)` registered before the bridge guard (parts tools have no bridge dependency).
- Tests confirmed: `tests/test_tools_parts.py` already covered `list_known_sources`, `bootstrap_known_source`, `index_library_source`, `search_parts`, `install_part`, and `parts_index_stats` at the HTTP/SQLite boundary. All 137 tests pass.
- Smoke check confirmed all 6 tools are reachable via plugin server: `['bootstrap_known_source', 'index_library_source', 'install_part', 'list_known_sources', 'parts_index_stats', 'search_parts']`.

---

### 3.6 Delete legacy backend chain
**Status**: Done (2026-05-02).

**Deleted (7 src files)**:
- `src/kicad_mcp/server.py` — legacy `create_server()`, no importers in active code
- `src/kicad_mcp/backends/factory.py` — `create_composite_backend()`, only called by legacy server
- `src/kicad_mcp/backends/composite.py` — `CompositeBackend`, only used by factory + legacy server
- `src/kicad_mcp/backends/ipc_backend.py` — `IPCBackend`, only registered by factory
- `src/kicad_mcp/backends/swig_backend.py` — `SWIGBackend`, only registered by factory
- `src/kicad_mcp/models/types.py` — type aliases, no active importers
- `src/kicad_mcp/utils/units.py` — unit helpers, no active importers

**Kept (corrected from original plan)**:
- `src/kicad_mcp/backends/subprocess_backend.py` — `SubprocessBackend`/`SubprocessBoardOps` classes stripped; helper functions (`_get_pcbnew`, `_run_pcbnew_script`, `_format_pcbnew_error`, `_malformed_board_message`, etc.) retained because `routing.py` imports them for `clean_board_for_routing`'s pcbnew subprocess fallback.

**Fixed before deletion**:
- `src/kicad_mcp/resources/definitions.py:10,16` — `CompositeBackend` → `BackendProtocol` (was a live type bug)
- `tests/conftest.py` — replaced `CompositeBackend`-based `mock_composite` with `MockProtocolBackend(BackendProtocol)`
- `tests/test_tools_parts.py` — removed `CompositeBackend` import/annotation

**Deleted test files**: `tests/test_factory.py`, `tests/test_composite.py`; `tests/test_subprocess_backend.py` stripped of `SubprocessBackend`/`SubprocessBoardOps` class tests.

**Result**: 110 tests pass. Plugin server registers 94 tools. No imports of deleted modules remain.

---

## Phase 4 — Features (**Complete** 2026-05-02)

All items in this phase were confirmed implemented during the 2026-05-02 audit. Several were already done before this sprint began.

### 4.1 `auto_place` — use pcbnew native bounding boxes when bridge is active
**Status**: Done (pre-existing). `PluginBoardOps.auto_place()` in `plugin_backend.py:217` calls the bridge via `_tcp_call("auto_place", ...)`. Bridge handler `_handle_auto_place()` exists in `kicad_mcp_bridge.py` and is in the dispatch table. The `auto_place` tool in `tools/board.py:396` calls `backend.get_board_modify_ops().auto_place()` first (bridge path) and falls back to `FileBoardOps.auto_place()` on `NotImplementedError`. Roadmap entry was stale.

---

### 4.2 `export_step` / `export_vrml` — 3D board model export
**Status**: Done (first documented 2026-04-28). Both tools are registered in `src/kicad_mcp/tools/export.py`. `export_step` calls `kicad-cli pcb export step`; `export_vrml` calls `kicad-cli pcb export vrml`. Both guard with `save_board()` before running kicad-cli and return `{"status": "success", "output_file": ...}` on success.

---

### 4.3 `validate_board` — file-based pre-flight checks without kicad-cli
**Status**: Done (first documented 2026-04-28). Tool is registered in `src/kicad_mcp/tools/drc.py`. Checks: Edge.Cuts outline present (error), duplicate reference designators (error), footprints at (0, 0) (warning), design rules block absent in `.kicad_pro` (warning). Returns `{"passed": bool, "violations": [...], "error_count": n, "warning_count": n, "checks_performed": [...]}`. Does not require kicad-cli.

---

### 4.4 `place_component` bulk API
**Status**: Done (pre-existing). `place_components_bulk()` exists on `PluginBoardOps` (`plugin_backend.py:226`, calls bridge via TCP) and `FileBoardOps` (`file_backend.py:1198`, reads/writes file once). Bridge handler `_handle_place_components_bulk()` registered in dispatch table at `kicad_mcp_bridge.py:778`. Roadmap entry was stale.

---

### 4.5 Schematic ERC — net-connectivity aware checks
**Status**: Done (pre-existing). `_build_connectivity()` in `file_backend.py:2436` builds schematic net connectivity via Union-Find over wire endpoints and labels. Called by `get_net_connections()` and `get_pin_net()`. Roadmap entry was stale.

---

### 4.6 `diff_board` — detect changes between two board snapshots
**Status**: Done (first documented 2026-04-28). Tool is registered in `src/kicad_mcp/tools/board.py`. Takes two `.kicad_pcb` paths, compares component positions and track counts. Returns `{"added_components": [...], "removed_components": [...], "moved_components": [...], "track_delta": n}`. Useful for confirming `autoroute` added tracks or `auto_place` moved all components.

---

## Phase 5 — Production Readiness (**Complete** 2026-05-03)

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
**Status**: Done (2026-05-03). 94 tools confirmed reachable on plugin server (verified via `create_plugin_server()` introspection after Phase 3.5 wired parts catalog).

| Group | Tools | Count |
|---|---|---|
| Phase 2 additions (previously documented) | `get_startup_checklist`, `estimate_board_size`, `validate_schematic_for_pcb`, `check_courtyard_overlaps`, `clear_routes` | +5 |
| Previously untracked (now documented) | `diff_board`, `validate_schematic_cli`, `validate_board`, `export_step`, `export_vrml` | +5 |
| Parts catalog (wired in 3.5) | `list_known_sources`, `bootstrap_known_source`, `index_library_source`, `search_parts`, `install_part`, `parts_index_stats` | +6 |

### 5.4 Linux/macOS bridge support
**Status**: Done (2026-04-12).
- `_get_kicad_python()` now probes snap prefix (`/snap/kicad/current/usr/bin/python3`)
  before falling back to `/usr/bin/python3` and `/usr/local/bin/python3`.
- `_run_pcbnew_script()` now sets `PYTHONPATH` to KiCad's dist-packages on Linux and
  macOS so system-package pcbnew installs are importable in the subprocess.

---

## Phase 6 — Placement Intelligence (**Shipped** 2026-06-15; planned 2026-05-09)

> **Status note (2026-10-06):** everything below shipped — the 6.1 connector
> tools and autoroute gate, the 6.2 `/build-pcb` sub-gates (since extended
> to 4a–4f with a placement-quality gate) and `auto_place` anchors, and the
> 6.3 `PlacementIntent` / sheet-hierarchy clustering. All four completion
> criteria below were discharged. The section is kept as the design record.

**Motivation.** During the `bt_audio_v1` /build-pcb session (2026-05-09), two of three external connectors (J2 JST battery, J3 3.5mm audio jack) ended up facing the board interior after `auto_place` + `autoroute`. Root cause is not a single bug — it's a layered gap:

1. **`sync_schematic_to_pcb` drops every footprint at origin with rotation 0°.** None of the schematic's structure (sheet hierarchy, symbol position, custom properties) carries forward.
2. **`auto_place` is a pure position optimizer.** No notion of "this connector belongs at the south edge" or "this face points outward." The footprint library *already encodes* this (the J3 footprint has a `(fp_text user "PCB edge" ... layer "Dwgs.User")` annotation; horizontal connectors have silk gaps on their mating face). No MCP tool reads any of it.
3. **No placement-quality gate exists.** `check_courtyard_overlaps` only proves the placement is non-colliding, not correct. The /build-pcb skill's Phase 4 lets the model move from `auto_place` straight to `autoroute` with every connector facing inward.
4. **"Hard rules" in skill markdown are honor-system.** "NEVER autoroute when courtyard overlaps present" is a rule the model can violate without resistance — `autoroute` itself accepts any board state.

The fix is a stack of three tiers, each useful on its own and ordered by leverage per effort.

---

### 6.1 Tier 1 — Connector orientation tooling

Four new tools that turn "use your judgment" into "the tool tells you and the next tool refuses without it." None require schematic-format changes; all read footprint metadata that's already in the standard library.

#### 6.1.1 `identify_edge_facing_connectors(path)` — detect which refs need edge placement
Scans every footprint in a `.kicad_pcb`. For each, classifies whether it has an external mating face by reading:
- `Dwgs.User` `"PCB edge"` annotations in the source `.kicad_mod` (definitive — placed by KiCad library maintainers)
- Silk-gap geometry: a one-sided break in the silk rectangle indicates a receptacle aperture
- Footprint name heuristics: `*Horizontal*`, `*_Receptacle_*`, `Connector_USB`, `Connector_Audio`, `Connector_JST`, `Connector_BarrelJack`
- `attr` flag heuristics: `through_hole exclude_from_pos_files` is a strong signal for a hand-soldered cable connector

Returns `[{ref, footprint, mating_face: "+y"|"-y"|"+x"|"-x", confidence: "high"|"medium", evidence: "..."}]`. The `mating_face` is in the *footprint's* local frame — the validator below applies the current `(at)` rotation to convert it to board frame.

#### 6.1.2 `validate_connector_orientations(path)` — placement-quality gate
For each ref returned by 6.1.1, compute the mating-face vector after the footprint's current rotation, then check whether that vector points toward the *closest* board edge (within an angular tolerance, e.g. ±30°). Returns:
```json
{
  "passed": false,
  "checked": 3,
  "violations": [
    {
      "ref": "J2",
      "current_face": "+y",
      "current_face_in_board_frame": "+y",
      "closest_edge": "south",
      "suggested_rotation": 180.0,
      "suggested_edge": "south"
    }
  ]
}
```
This is the gate that closes the placement-quality hole. It's also the validator that 6.1.4 enforces.

#### 6.1.3 `place_at_edge(path, reference, edge, offset_mm=2.0)` — outward-facing placement
Computes correct rotation + position so the named ref's mating face points outward at the given edge (`"north"|"south"|"east"|"west"`), with `offset_mm` clearance from the courtyard to the edge. Replaces the manual rotation arithmetic the model currently does (and gets wrong). Internally calls `move_component` after computing target.

#### 6.1.4 Tool-level enforcement on `autoroute`
`autoroute` records a state-version hash of the .kicad_pcb at start of each call. `validate_connector_orientations` records its result + the same hash. If the latest validator result for the current hash is missing or `passed=false`, `autoroute` refuses with a structured error including the violations. The user-facing rule "never autoroute with bad placement" becomes mechanically enforced rather than honor-system.

Same pattern can extend to: `check_courtyard_overlaps.passed=false` blocking `autoroute`, `validate_schematic_for_pcb` blocking `sync_schematic_to_pcb`, etc. — the orientation gate is the first one to wire up because it's where today's failure happened.

#### 6.1.5 Tests
- `tests/test_identify_edge_facing_connectors.py` — fixture footprints (`USB_C_Receptacle_Amphenol`, `Jack_3.5mm_CUI_SJ1-3523N_Horizontal`, `JST_PH_S2B-PH-K_*Horizontal`, plus an SMT chip that should NOT be flagged) covering each detection mechanism.
- `tests/test_validate_connector_orientations.py` — fixtures with connectors at each rotation × each edge proximity, including the case where two edges are equidistant.
- `tests/test_place_at_edge.py` — verifies rotation arithmetic for all four edges × four starting rotations × two footprint asymmetries.
- `tests/test_autoroute_gate.py` — confirms `autoroute` refuses without prior validation pass; confirms it accepts after pass; confirms re-`move_component` invalidates the prior pass.

---

### 6.2 Tier 2 — `/build-pcb` Phase 4 sub-gates

The skill's Phase 4 is currently `sync → set_rules → auto_place → check_overlaps`. Split into ordered sub-phases with their own gates so the model cannot skip from "synced" to "no overlaps" without explicit orientation work.

#### 6.2.1 Phase 4 rewrite in `.claude/commands/build-pcb.md`
- **4a — Sync & survey:** `sync_schematic_to_pcb`, `set_board_design_rules`, `identify_edge_facing_connectors`. Report names every detected edge-facing ref. Gate: every detected ref has a planned edge assignment.
- **4b — Anchor connectors:** `place_at_edge` for each ref from 4a. Re-run `validate_connector_orientations`. Gate: `passed == true`.
- **4c — Bulk placement:** `auto_place` (with anchored refs locked — see 6.2.2). Gate: every non-anchored ref has a position.
- **4d — Overlap check:** `check_courtyard_overlaps`. Resolve via `move_component`. Gate: `passed == true`.
- **4e — Final orientation re-check:** `validate_connector_orientations` once more (covers cases where overlap-resolution moves rotated a connector). Gate: `passed == true`.

Each sub-gate gets its own report block in the skill so the model cannot collapse them.

#### 6.2.2 `auto_place` accepts an `anchors` parameter
`auto_place(path, anchors=[ref, ref, ...])` skips listed refs in its placement loop, treating them as fixed obstacles. Required by 4c so that the connector orientations set in 4b survive bulk placement.

#### 6.2.3 `CLAUDE.md` PCB Design Protocol updates
- Phase 4 row in the table replaced with five rows (4a–4e) showing each gate condition.
- Hard rules updated: "NEVER call autoroute when `validate_connector_orientations.passed == false`" added alongside the existing courtyard rule.
- Reference to memory entry on edge-facing connectors (this becomes important institutional knowledge).

---

### 6.3 Tier 3 — Schematic-driven placement intent

The schematic currently dictates connectivity but is dropped on the floor for layout. Fix that by letting symbols carry placement intent that flows through `sync_schematic_to_pcb` to seed initial PCB state, and by clustering placement using sheet hierarchy that's already in the data.

#### 6.3.1 `PlacementIntent` symbol property
Custom property on schematic symbols, opaque string, parsed by `sync_schematic_to_pcb`:
- `edge:north` / `edge:south` / `edge:east` / `edge:west` — anchor at the named edge with outward-facing rotation, equivalent to a `place_at_edge` call after sync
- `cluster:<name>` — keep refs sharing the cluster name geometrically grouped; `auto_place` reads the cluster annotation and treats them as a soft constraint
- `lock` — never move once placed (combined with `edge:*` for fixed connectors)

#### 6.3.2 `sync_schematic_to_pcb` reads `PlacementIntent`
After importing footprints + nets, parse `PlacementIntent` for each component. For `edge:*` values, call `place_at_edge` with computed offset. For `cluster:*`, attach metadata to the footprint's `(property "ClusterId" "...")` so `auto_place` can cluster by it. Symbols without the property fall through to today's behavior (origin placement).

This is what makes the schematic actually contribute to layout. Today the schematic is a netlist generator; with this, it's a placement plan.

#### 6.3.3 `auto_place` respects sheet hierarchy
Components in the same hierarchical sheet cluster spatially on the PCB. The sheet hierarchy is already in `.kicad_sch`; `sync_schematic_to_pcb` already preserves the path. `auto_place` reads the `(path ...)` from each component instance and treats common-prefix paths as soft clustering hints. No new file format.

#### 6.3.4 Tests
- `tests/test_sync_placement_intent.py` — fixture schematic with `PlacementIntent: edge:south` symbol → confirm post-sync footprint is at south edge with correct rotation.
- `tests/test_auto_place_clustering.py` — fixture with three sheets → confirm components within a sheet land closer to each other than to other sheets' components.

---

### 6.4 Sequencing & success criteria

Tiers are independently shippable. Recommended order: **6.1 → 6.2 → 6.3** (each is a prerequisite for the next becoming useful).

**Phase 6 is complete when:**
- Re-running `/build-pcb` on `bt_audio_v1` produces J1, J2, J3 facing outward without manual rotation calls.
- `validate_connector_orientations` returns `passed=true` after Phase 4d on every example project in `examples/`.
- `autoroute` returns a clear refusal error if invoked on a board with failing orientation validation.
- A schematic with `PlacementIntent: edge:south` on a connector symbol produces that connector at the south edge after `sync_schematic_to_pcb`, with no manual `place_at_edge` call.

---

## Quick Reference: Known Technical Debt

| Item | File | Severity |
|---|---|---|
| ~~`mypy` type coverage incomplete~~ | ~~`src/` (all modules)~~ | **FIXED** 2026-07-12 — whole-repo `mypy src/` is clean and BLOCKING in CI; hot files additionally held to `mypy --strict` (also blocking) |
| ~~`definitions.py` type annotation `backend: CompositeBackend`~~ | ~~`src/kicad_mcp/resources/definitions.py:16`~~ | **FIXED** 2026-05-02 (3.6) |
| ~~`kicad_mcp.__main__` legacy entry point~~ | ~~`src/kicad_mcp/__main__.py`~~ | **FIXED** 2026-05-02 |
| `reload_board` falls through to `Refresh()` on KiCad 9 — does not rebuild track cache | `kicad_mcp_bridge.py:_handle_reload_board` | Low — only callers (`import_ses`, ex-`clear_routes`) already mutate the live board, so the reload is redundant. Documented in 3.4 above. |
| ~~`auto_place` uses file-based bounds (not pcbnew native)~~ | ~~`tools/board.py`, `file_backend.py`~~ | **FIXED** (pre-existing) |
| ~~Linux pcbnew path detection incomplete~~ | ~~`backends/subprocess_backend.py`~~ | **MOOT** — file deleted in 3.6 |
| ~~Bridge wx bare-name bug + SetDrill rename~~ | ~~`kicad_mcp_bridge.py`~~ | **FIXED** 2026-05-01 |
| ~~`open_kicad` board-switch async~~ | ~~`tools/project.py`~~ | **FIXED** (feat/plugin-backend) |
| ~~`kicad-cli pcb drc` overwrites `.kicad_pro`~~ | ~~`backends/cli_backend.py`~~ | **FIXED** `0eb29e4` |
