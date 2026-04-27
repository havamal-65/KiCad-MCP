#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install the KiCad MCP bridge plugin into KiCad 9 on Windows.

.DESCRIPTION
    Copies kicad_mcp_bridge.py as __init__.py into the KiCad 9 PCM plugins
    directory ([MyDocuments]\KiCad\9.0\3rdparty\plugins\kicad_mcp_bridge\)
    and patches pcbnew.json so KiCad auto-loads the bridge on every pcbnew
    startup without needing a manual "Refresh Plugins" step.

    Requires PowerShell 7+ (pwsh). Run with:
        pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1

.NOTES
    The install target is the PCM plugins path, not the legacy scripting\plugins
    directory. KiCad 9 auto-scans 3rdparty\plugins at startup; scripting\plugins
    is only accessible via the Scripting Console.
    Safe to run multiple times (idempotent).
#>

#Requires -Version 7

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Use [MyDocuments] so this works on machines where Documents is on OneDrive
$docsDir     = [Environment]::GetFolderPath("MyDocuments")
$pluginsRoot = Join-Path $docsDir "KiCad\9.0\3rdparty\plugins"
$bridgeDir   = Join-Path $pluginsRoot "kicad_mcp_bridge"

# KiCad 9 imports plugin directories as Python packages via __init__.py
$targetFile  = Join-Path $bridgeDir "__init__.py"

$pcbnewJson  = Join-Path $env:APPDATA "kicad\9.0\pcbnew.json"
$sourceFile  = Join-Path $PSScriptRoot "kicad_mcp_bridge.py"

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

if (-not (Test-Path $sourceFile)) {
    Write-Error "Source not found: $sourceFile — run from the kicad_plugin directory."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 1a — DELETE all AppData scripting\plugins bridge copies
#
# ROOT CAUSE FIX (2026-03-29):
# KiCad loads scripting\plugins in the project-manager Python context, BEFORE
# the PCB editor opens.  In that context `import pcbnew` fails (pcbnew is only
# available inside the PCB editor).  The failed import caches `kicad_mcp_bridge`
# in sys.modules with no TCP server started.  When the PCB editor later tries
# to load the same module from 3rdparty\plugins, Python returns the cached
# (broken) object — the server is never started and port 9760 never opens.
#
# Fix: the bridge must ONLY live in 3rdparty\plugins.  Any scripting\plugins
# copy — single-file or directory — must be fully deleted, including pycache.
# ---------------------------------------------------------------------------

$scriptingPlugins = Join-Path $env:APPDATA "kicad\9.0\scripting\plugins"

# Single-file legacy copy
$staleFile = Join-Path $scriptingPlugins "kicad_mcp_bridge.py"
if (Test-Path $staleFile) {
    Remove-Item -Force $staleFile
    Write-Host "Deleted stale scripting plugin file: $staleFile"
}

# Stale compiled bytecode for single-file copy (no corresponding .py → skip by Python, but remove anyway)
$stalePyc = Join-Path $scriptingPlugins "__pycache__\kicad_mcp_bridge.cpython-311.pyc"
if (Test-Path $stalePyc) {
    Remove-Item -Force $stalePyc
    Write-Host "Deleted stale pyc: $stalePyc"
}

# Subdirectory copy — DELETE the entire directory so it cannot shadow 3rdparty
$staleDir = Join-Path $scriptingPlugins "kicad_mcp_bridge"
if (Test-Path $staleDir) {
    Remove-Item -Recurse -Force $staleDir
    Write-Host "Deleted stale scripting plugin directory: $staleDir"
}

# ---------------------------------------------------------------------------
# Step 1b — create bridge subdirectory and copy as __init__.py
# ---------------------------------------------------------------------------

if (-not (Test-Path $pluginsRoot)) {
    Write-Host "Creating plugins directory: $pluginsRoot"
    New-Item -ItemType Directory -Path $pluginsRoot -Force | Out-Null
}

if (-not (Test-Path $bridgeDir)) {
    New-Item -ItemType Directory -Path $bridgeDir | Out-Null
    Write-Host "Created: $bridgeDir"
}

Copy-Item -Force $sourceFile $targetFile
Write-Host "Installed: $targetFile"

# ---------------------------------------------------------------------------
# Step 2 — patch pcbnew.json so KiCad records the plugin in action_plugins
# ---------------------------------------------------------------------------

# KiCad stores plugin paths with forward slashes
$pluginPath = $bridgeDir.Replace("\", "/")

if (Test-Path $pcbnewJson) {
    $cfg = Get-Content $pcbnewJson -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    Write-Host "pcbnew.json not found — will create: $pcbnewJson"
    $pcbnewDir = Split-Path $pcbnewJson
    if (-not (Test-Path $pcbnewDir)) {
        New-Item -ItemType Directory -Path $pcbnewDir -Force | Out-Null
    }
    $cfg = [pscustomobject]@{}
}

$existing = @()
if ($cfg.PSObject.Properties.Name -contains "action_plugins") {
    $existing = @($cfg.action_plugins)
}

$alreadyPresent = $existing | Where-Object {
    $_.PSObject.Properties.Name -contains "path" -and $_.path -eq $pluginPath
}

if ($alreadyPresent) {
    Write-Host "action_plugins entry already present in pcbnew.json"
} else {
    $entry = [pscustomobject]@{ path = $pluginPath; show_button = $false }
    $cfg | Add-Member -NotePropertyName "action_plugins" `
                      -NotePropertyValue (@($existing) + @($entry)) `
                      -Force
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $pcbnewJson -Encoding UTF8
    Write-Host "Patched pcbnew.json with action_plugins entry"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

$port = $env:KICAD_MCP_PLUGIN_PORT ?? "9760"

Write-Host ""
Write-Host "Installation complete."
Write-Host "  Plugin directory : $bridgeDir"
Write-Host "  Entry point      : $targetFile"
Write-Host "  Port             : $port"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Close all KiCad / pcbnew windows."
Write-Host "  2. Open pcbnew and load any board."
Write-Host "  3. Check: Test-NetConnection -ComputerName localhost -Port 9760"
Write-Host "  4. Close and reopen pcbnew (second cold start)."
Write-Host "  5. Check port again — must still be open."
Write-Host "  6. Restart the MCP server, then call get_backend_info()."
