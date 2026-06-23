---
layout: default
title: Roadmap
nav_order: 5
---

# Development Roadmap
{: .no_toc }

*Last updated: 2026-05-07*

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Guiding Principles

- **Plugin bridge is primary.** The subprocess/file fallback exists only for environments where KiCad isn't running. Don't let it become the happy path.
- **Fix tools, never work around them.** If a tool is broken, fix the tool. Scripts that bypass MCP are forbidden.
- **No speculative abstractions.** Add features when there's a concrete use-case, not "just in case".
- **Test at the boundary.** Unit tests for pure logic; E2E tests via the MCP protocol for tool behavior.

---

## Phase 1 — Stability ✅ Complete

Bug fixes and fragile behavior resolutions. All resolved as of 2026-05-01.

| Item | Status |
|------|--------|
| Fix bridge reinstall (wx bare-name bug + KiCad 9 API renames) | Done 2026-05-01 |
| Fix `open_kicad` board-switch async race | Done (pre-existing) |
| Add pytest test suite | Done 2026-04-12 |
| Merge `feat/plugin-backend` → `main` | Done 2026-04-27 |

---

## Phase 2 — Workflow Improvements ✅ Complete (2026-04-12)

Five new MCP tools and targeted enhancements.

| Tool | Description |
|------|-------------|
| `get_startup_checklist` | Pre-flight environment check — 6 ordered gates |
| `estimate_board_size` | Automatic PCB dimension estimation from footprint list |
| `validate_schematic_for_pcb` | Schematic readiness gate before sync |
| `check_courtyard_overlaps` | AABB overlap detection between component courtyards |
| `clear_routes` | Non-destructive rip-up preserving footprint placement |

Pipeline enhancements: `pcb_pipeline` gained mandatory Step 0 pre-flight gate; `auto_place` added `utilization_pct`; `export_pdf` surfaces actionable errors; `CLAUDE.md` updated with 7-phase professional table.

---

## Phase 3 — Architecture ✅ Complete (2026-05-02)

| Item | Status |
|------|--------|
| Retire `kicad_mcp.__main__` legacy entry point | Done 2026-05-02 |
| `BackendProtocol` → concrete base class | Done (pre-existing) |
| Bridge watchdog — reconnect after KiCad restart | Done (pre-existing) |
| `PluginDirectBackend` — `reload_board` dispatch | Partially done (caveat in 3.4) |
| Wire `parts` catalog into plugin server | Done 2026-05-02 |
| Delete legacy backend chain | Done 2026-05-02 |

---

## Phase 4 — Features ✅ Complete (2026-05-02)

All items confirmed implemented during the 2026-05-02 audit.

| Item | Status |
|------|--------|
| `auto_place` — use pcbnew native bounding boxes | Done (pre-existing) |
| `export_step` / `export_vrml` — 3D model export | Done 2026-04-28 |
| `validate_board` — file-based pre-flight checks | Done 2026-04-28 |
| `place_component` bulk API | Done (pre-existing) |
| Schematic ERC — net-connectivity aware checks | Done (pre-existing) |
| `diff_board` — detect changes between board snapshots | Done 2026-04-28 |

---

## Phase 5 — Production Readiness ✅ Complete (2026-05-03)

| Item | Status |
|------|--------|
| Packaging — single-file bridge installer via pip | Done 2026-04-12 |
| CI/CD — GitHub Actions (pytest + mypy + PyPI publish) | Done 2026-04-12 |
| MCP tool count audit (102 tools confirmed) | Done 2026-05-03 |
| Linux/macOS bridge support | Done 2026-04-12 |

---

## Known Technical Debt

| Item | File | Severity |
|------|------|----------|
| `mypy` type coverage incomplete | `src/` (all modules) | Medium — CI runs informational only; enforce strict mode as a future phase |
| `reload_board` falls through to `Refresh()` on KiCad 9 | `kicad_mcp_bridge.py` | Low — only callers already mutate the live board directly |
