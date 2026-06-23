---
layout: home
title: Home
nav_order: 1
---

# KiCad MCP Server

A pure Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for KiCad EDA automation. Enable AI assistants like Claude, Cursor, and others to interact with KiCad projects programmatically.

## Overview

KiCad MCP Server provides a standardized interface for AI assistants to read, analyze, and modify KiCad electronic design automation (EDA) files. It supports a plugin backend architecture to work with KiCad in different environments.

## Key Features

**102 MCP Tools** across 9 categories:

| Category | Count | Description |
|----------|-------|-------------|
| 📋 Project Management | 14 | Create/open projects, metadata, startup checklist |
| 📐 Schematic Operations | 26 | Place/wire/annotate components, net analysis, sync to PCB |
| 🔌 PCB Board Operations | 16 | Read boards, place/route components, auto-placement |
| 📚 Library Search | 8 | Search symbols/footprints, suggest footprints |
| 📦 Library Management | 9 | Clone repos, import symbols/footprints |
| ✅ Design Rule Checks | 10 | DRC/ERC, connector orientation validation |
| 📤 Export Operations | 7 | Gerbers, drill, BOM, PDF, 3D STEP/VRML |
| 🔀 Auto-Routing | 6 | FreeRouting integration, clear routes |
| 🔍 Parts Catalog | 6 | Index and search third-party KiCad libraries |

## Plugin Backend Architecture

`PluginDirectBackend` routes each operation to the right subsystem — no fallback ambiguity:

| Operation | Backend |
|-----------|---------|
| Board read/write | TCP bridge → KiCad's embedded `pcbnew` Python |
| Schematic read/write | Pure Python file backend |
| DRC / export | `kicad-cli` subprocess |
| Library search/management | Pure Python file backend |

## Use Cases

- **AI-Assisted PCB Design**: Let AI assistants help design and review circuits
- **Automated Quality Checks**: Run DRC/ERC as part of CI/CD pipelines
- **Batch Processing**: Automate repetitive design tasks across multiple projects
- **Design Analysis**: Extract and analyze design data programmatically
- **Documentation Generation**: Auto-generate BOMs, netlists, and design docs
- **Design Migration**: Convert or update designs programmatically

## Claude Code Skill: `/build-pcb`

Invoke `/build-pcb [project description]` in Claude Code to start a **professional, phased PCB design session** mirroring IPC/JEDEC industry practice:

| Phase | Name | Gate condition |
|-------|------|----------------|
| 1 | Environment & Requirements | `get_startup_checklist.ready_for_pcb` |
| 2 | Schematic Capture | All components placed, ≥1 net |
| 3 | Schematic Verification | `validate_schematic_for_pcb.ready_for_pcb_sync`, ERC clean |
| 4a | Sync & Survey | Edge-facing connectors identified |
| 4b | Anchor Connectors | `validate_connector_orientations.passed` |
| 4c | Bulk Placement | All non-anchored refs placed |
| 4d | Overlap Check | `check_courtyard_overlaps.passed` |
| 4e | Final Orientation Re-check | `validate_connector_orientations.passed` |
| 5 | Routing | Zero unrouted connections |
| 6 | Design Verification | `run_drc.passed` |
| 7 | Manufacturing Outputs | All six export files generated |

---

[Get started with Installation →](installation)
