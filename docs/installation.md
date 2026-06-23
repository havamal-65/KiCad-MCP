---
layout: default
title: Installation
nav_order: 2
---

# Installation
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Requirements

- Python 3.10 or higher
- KiCad 7.0+ (optional, depending on operation type)

## Install from PyPI

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install kicad-mcp
```

## Install from Source

```bash
git clone https://github.com/havamal-65/KiCad-MCP.git
cd KiCad-MCP
pip install -e .
```

## Optional Dependencies

### Auto-Routing (FreeRouting)

Auto-routing requires Java and FreeRouting:

1. Install Java Runtime Environment (JRE)
2. Download [FreeRouting JAR](https://github.com/freerouting/freerouting/releases)
3. Either:
   - Place the JAR in `~/.kicad-mcp/freerouting/`
   - Set `KICAD_MCP_FREEROUTING_JAR` to the JAR path
   - Provide `freerouting_jar` parameter directly to the tool

### Development

```bash
pip install kicad-mcp[dev]
```

---

## Plugin Backend Setup

The plugin backend gives the MCP direct live access to `pcbnew`'s in-memory board data while KiCad is open. It works on **Windows, Linux, and macOS** with KiCad 9.

The install scripts:
1. Remove any stale bridge copies from `scripting/plugins/` (stale copies cause a `sys.modules` conflict that silently prevents the bridge from starting)
2. Install `kicad_mcp_bridge.py` as `__init__.py` in KiCad's PCM plugins directory
3. Patch `pcbnew.json` so KiCad auto-loads the bridge on every pcbnew startup

### Windows (PowerShell 7+)

```powershell
pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1
```

Installs to: `[MyDocuments]\KiCad\9.0\3rdparty\plugins\kicad_mcp_bridge\`

### Linux / macOS

```bash
bash kicad_plugin/install_bridge.sh
```

Installs to:
- **Linux:** `~/.local/share/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/`
- **macOS:** `~/Library/Preferences/kicad/9.0/3rdparty/plugins/kicad_mcp_bridge/`

### After Installing (all platforms)

1. Close all KiCad / pcbnew windows
2. Open pcbnew and load your board
3. Verify the bridge is running:
   - **Windows:** `Test-NetConnection -ComputerName localhost -Port 9760`
   - **Linux/macOS:** `python3 -c "import socket; s=socket.create_connection(('localhost',9760),2); print('bridge OK'); s.close()"`
4. Start the MCP server: `python -m kicad_mcp_plugin`

> **Reinstalling after source updates:** Re-run the install script, then close and reopen pcbnew. Check `bridge_startup.log` in the plugin directory for startup diagnostics.

---

## Quick Start

### Run the Server

#### Stdio Transport (for Claude Desktop, Cursor, etc.)

```bash
python -m kicad_mcp_plugin
```

#### SSE Transport (for web clients)

```bash
python -m kicad_mcp_plugin --transport sse --sse-host 127.0.0.1 --sse-port 8765
```

---

## Client Integration

### Claude Code (recommended)

A `.mcp.json` is included at the repo root. Claude Code picks it up automatically when you open the folder — no manual config required.

### Codex CLI

**Windows:**

```powershell
codex mcp add kicad `
  --env KICAD_MCP_LOG_LEVEL=INFO `
  -- "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -ExecutionPolicy Bypass `
  -NonInteractive `
  -File "C:\path\to\KiCad-MCP\run_plugin.ps1"
```

**macOS / Linux:**

```bash
codex mcp add kicad \
  --env KICAD_MCP_LOG_LEVEL=INFO \
  -- /path/to/KiCad-MCP/run_plugin.sh
```

### Claude Desktop — Windows

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "args": [
        "-ExecutionPolicy", "Bypass",
        "-NonInteractive",
        "-File", "C:\\path\\to\\KiCad-MCP\\run_plugin.ps1"
      ],
      "env": {
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Claude Desktop — macOS / Linux

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `~/.config/Claude/claude_desktop_config.json` (Linux):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/path/to/KiCad-MCP/run_plugin.sh",
      "env": {
        "KICAD_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings using `run_plugin.ps1` (Windows) or `run_plugin.sh` (macOS/Linux) as shown above.

---

## Bootstrap Scripts

The repo ships bootstrap scripts for the plugin entry point:

- **`run_plugin.ps1`** — Windows (PowerShell 7+)
- **`run_plugin.sh`** — macOS / Linux

These automatically create a virtual environment and install all dependencies on first run, and they clear inherited `PYTHONHOME`/`PYTHONPATH` overrides so MCP clients use the repo's venv instead of global Python packages.

---

[View Available Tools →](tools)
