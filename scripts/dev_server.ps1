<#
.SYNOPSIS
  Dev hot-reload MCP server (streamable-http) for KiCad-MCP.

.DESCRIPTION
  Runs `kicad_mcp_plugin` under watchfiles so any edit under src/ auto-restarts
  the server. Claude Code — pointed at .mcp.dev.json — auto-reconnects on the
  next tool call (HTTP transport, exponential backoff). No manual `/mcp`, no
  process-kill dance.

  End users are unaffected: the committed .mcp.json still uses stdio.

.EXAMPLE
  pwsh -File scripts/dev_server.ps1
  pwsh -File scripts/dev_server.ps1 -Port 8765

  Then, in another terminal, launch Claude scoped to the dev config:
    claude --strict-mcp-config --mcp-config .mcp.dev.json
#>
param(
    [int]$Port = 8765,
    [string]$BindHost = "127.0.0.1"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python — create the venv first." }

$serverCmd = "$python -m kicad_mcp_plugin --transport streamable-http --host $BindHost --port $Port"
$srcPath = Join-Path $root "src"

Write-Host "Dev MCP server (hot-reload): http://${BindHost}:${Port}/mcp" -ForegroundColor Green
Write-Host "Watching $srcPath — edit & save to auto-restart. Ctrl+C to stop." -ForegroundColor Green
Write-Host "Point Claude at it: claude --strict-mcp-config --mcp-config .mcp.dev.json" -ForegroundColor DarkGray

& $python -m watchfiles $serverCmd $srcPath
