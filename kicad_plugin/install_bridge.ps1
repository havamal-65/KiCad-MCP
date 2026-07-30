#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install the KiCad MCP bridge plugin into KiCad on Windows (9.0, 10.0, …).

.DESCRIPTION
    Copies kicad_mcp_bridge.py as __init__.py into each target KiCad version's
    PCM plugins directory (…\KiCad\<v>\3rdparty\plugins\kicad_mcp_bridge\) and
    patches that version's pcbnew.json so KiCad auto-loads the bridge on every
    pcbnew startup without a manual "Refresh Plugins" step.

    Target versions default to EVERY KiCad detected under Program Files. Pass
    -KicadVersion to target specific ones.

    The 3rdparty root is resolved config-first: a user-relocated
    KICAD<major>_3RD_PARTY in that version's kicad_common.json wins, otherwise
    the OS Documents dir ([Environment]::GetFolderPath("MyDocuments"), which is
    correct on OneDrive-redirected machines).

    Requires PowerShell 7+ (pwsh). Run with:
        pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1
        pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1 -KicadVersion 10.0

.NOTES
    The install target is the PCM plugins path, not the legacy scripting\plugins
    directory. KiCad auto-scans 3rdparty\plugins at startup; scripting\plugins is
    only accessible via the Scripting Console. Safe to run multiple times.
#>

#Requires -Version 7

[CmdletBinding()]
param(
    # KiCad version(s) to target, e.g. 10.0 or 9.0. Default: all detected.
    [string[]]$KicadVersion
)

$ErrorActionPreference = "Stop"

$sourceFile = Join-Path $PSScriptRoot "kicad_mcp_bridge.py"
$docsDir    = [Environment]::GetFolderPath("MyDocuments")

if (-not (Test-Path $sourceFile)) {
    Write-Error "Source not found: $sourceFile — run from the kicad_plugin directory."
    exit 1
}

# ---------------------------------------------------------------------------
# Version detection + path resolution
# ---------------------------------------------------------------------------

function Get-InstalledKicadVersions {
    # Every …\KiCad\<X.Y>\bin\pcbnew.exe under Program Files, newest-first.
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    $versions = [System.Collections.Generic.List[string]]::new()
    foreach ($root in $roots) {
        $kicadRoot = Join-Path $root "KiCad"
        if (-not (Test-Path $kicadRoot)) { continue }
        Get-ChildItem -Directory $kicadRoot | ForEach-Object {
            if ($_.Name -match '^\d+\.\d+$' -and
                (Test-Path (Join-Path $_.FullName "bin\pcbnew.exe"))) {
                if (-not $versions.Contains($_.Name)) { $versions.Add($_.Name) }
            }
        }
    }
    return $versions | Sort-Object -Property { [version]$_ } -Descending
}

function Resolve-ThirdPartyDir {
    param([string]$Version)
    # Config-first: KICAD<major>_3RD_PARTY from kicad_common.json, else Documents.
    $major = $Version.Split('.')[0]
    $common = Join-Path $env:APPDATA "kicad\$Version\kicad_common.json"
    if (Test-Path $common) {
        try {
            $cfg = Get-Content $common -Raw -Encoding UTF8 | ConvertFrom-Json
            $vars = $cfg.environment.vars
            $relocated = $vars."KICAD${major}_3RD_PARTY"
            if ($relocated) { return $relocated }
        } catch {
            # Malformed config — fall through to the Documents default.
        }
    }
    return (Join-Path $docsDir "KiCad\$Version\3rdparty")
}

# ---------------------------------------------------------------------------
# Per-version install
# ---------------------------------------------------------------------------

function Install-BridgeForVersion {
    param([string]$Version)

    $thirdParty  = Resolve-ThirdPartyDir -Version $Version
    $pluginsRoot = Join-Path $thirdParty "plugins"
    $bridgeDir   = Join-Path $pluginsRoot "kicad_mcp_bridge"
    $targetFile  = Join-Path $bridgeDir "__init__.py"
    $pcbnewJson  = Join-Path $env:APPDATA "kicad\$Version\pcbnew.json"

    Write-Host "=== KiCad $Version ==="
    Write-Host "  3rdparty : $thirdParty"

    # Step 1a — DELETE all AppData scripting\plugins bridge copies for this
    # version. KiCad loads scripting\plugins in the project-manager Python
    # context BEFORE the PCB editor opens; there `import pcbnew` fails and the
    # broken module is cached in sys.modules, so the real 3rdparty copy never
    # starts its TCP server. The bridge must live ONLY in 3rdparty\plugins.
    $scriptingPlugins = Join-Path $env:APPDATA "kicad\$Version\scripting\plugins"
    $staleFile = Join-Path $scriptingPlugins "kicad_mcp_bridge.py"
    if (Test-Path $staleFile) {
        Remove-Item -Force $staleFile
        Write-Host "  Deleted stale scripting plugin file: $staleFile"
    }
    $stalePyc = Join-Path $scriptingPlugins "__pycache__\kicad_mcp_bridge.cpython-311.pyc"
    if (Test-Path $stalePyc) {
        Remove-Item -Force $stalePyc
        Write-Host "  Deleted stale pyc: $stalePyc"
    }
    $staleDir = Join-Path $scriptingPlugins "kicad_mcp_bridge"
    if (Test-Path $staleDir) {
        Remove-Item -Recurse -Force $staleDir
        Write-Host "  Deleted stale scripting plugin directory: $staleDir"
    }

    # Step 1b — create bridge subdirectory and copy as __init__.py
    if (-not (Test-Path $pluginsRoot)) {
        New-Item -ItemType Directory -Path $pluginsRoot -Force | Out-Null
        Write-Host "  Creating plugins directory: $pluginsRoot"
    }
    if (-not (Test-Path $bridgeDir)) {
        New-Item -ItemType Directory -Path $bridgeDir | Out-Null
    }
    Copy-Item -Force $sourceFile $targetFile
    Write-Host "  Installed: $targetFile"

    # Step 2 — patch pcbnew.json so KiCad records the plugin in action_plugins
    $pluginPath = $bridgeDir.Replace("\", "/")
    if (Test-Path $pcbnewJson) {
        $cfg = Get-Content $pcbnewJson -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        Write-Host "  pcbnew.json not found — will create: $pcbnewJson"
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
        Write-Host "  action_plugins entry already present in pcbnew.json"
    } else {
        $entry = [pscustomobject]@{ path = $pluginPath; show_button = $false }
        $cfg | Add-Member -NotePropertyName "action_plugins" `
                          -NotePropertyValue (@($existing) + @($entry)) `
                          -Force
        $cfg | ConvertTo-Json -Depth 10 | Set-Content $pcbnewJson -Encoding UTF8
        Write-Host "  Patched pcbnew.json with action_plugins entry"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if ($KicadVersion) {
    $targets = $KicadVersion
} else {
    $targets = Get-InstalledKicadVersions
    if (-not $targets) {
        Write-Error "No KiCad install found under Program Files. Pass -KicadVersion explicitly."
        exit 1
    }
}

foreach ($v in $targets) {
    Install-BridgeForVersion -Version $v
}

$port = $env:KICAD_MCP_PLUGIN_PORT ?? "9760"

Write-Host ""
Write-Host "Installation complete for: $($targets -join ', ')"
Write-Host "  Port : $port"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Close all KiCad / pcbnew windows."
Write-Host "  2. Open pcbnew and load any board."
Write-Host "  3. Check: Test-NetConnection -ComputerName localhost -Port $port"
Write-Host "  4. Close and reopen pcbnew (second cold start)."
Write-Host "  5. Check port again — must still be open."
Write-Host "  6. Restart the MCP server, then call get_backend_info()."
