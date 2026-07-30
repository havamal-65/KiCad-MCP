# U4 local smoke — the no-sandbox fallback oracle for REQ-U4-007's substance.
#
#   pwsh -File installer\local_smoke.ps1
#
# Windows Sandbox proves "pristine OS"; when it isn't available this script
# proves the two things that actually matter, on the real machine:
#   1. the installed app runs with the system Python SCRUBBED from the
#      environment (PATH stripped of python dirs, PYTHONHOME/PYTHONPATH
#      cleared) — the bundle depends on nothing outside itself;
#   2. the installer lifecycle is clean (silent install -> shortcuts/dir ->
#      silent uninstall -> nothing left).
# Run sandbox_smoke.ps1 instead whenever the Windows Sandbox feature is on.

$ErrorActionPreference = "Stop"
$Out = Join-Path $PSScriptRoot "out"
$App = "$env:LOCALAPPDATA\Programs\KiCad-MCP Launcher"
$Shortcut = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\KiCad-MCP Launcher.lnk"
$Port = 8997

$Artifact = Get-ChildItem (Join-Path $Out "KiCad-MCP-Launcher-Setup-*.exe") |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Artifact) { throw "no installer artifact in $Out — run installer\build.ps1 first" }

# A PATH with every python-ish dir removed + a python-free env block.
$cleanPath = ($env:PATH -split ";" | Where-Object {
    $_ -and $_ -notmatch "python|\.venv|Scripts" }) -join ";"
$env:KICAD_MCP_SMOKE_PATH = $cleanPath  # for visibility in failures

function Invoke-Scrubbed([string]$File, [string[]]$Arguments, [int]$WaitSeconds) {
    # NB: the parameter must NOT be named $Args — that collides with
    # PowerShell's automatic $args and silently drops every argument.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    foreach ($a in $Arguments) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables["PATH"] = $cleanPath
    $psi.EnvironmentVariables.Remove("PYTHONHOME") | Out-Null
    $psi.EnvironmentVariables.Remove("PYTHONPATH") | Out-Null
    $psi.EnvironmentVariables.Remove("VIRTUAL_ENV") | Out-Null
    $p = [System.Diagnostics.Process]::Start($psi)
    if ($WaitSeconds -gt 0) { Start-Sleep -Seconds $WaitSeconds }
    return $p
}

Write-Host "1. silent install of $($Artifact.Name)..."
$p = Start-Process -FilePath $Artifact.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "installer exit code $($p.ExitCode)" }
if (-not (Test-Path "$App\KiCad-MCP Launcher.exe")) { throw "app exe missing after install" }
if (-not (Test-Path "$App\kicad-mcp-server.exe")) { throw "server exe missing after install" }
if (-not (Test-Path $Shortcut)) { throw "start-menu shortcut missing after install" }
Write-Host "   install dir + shortcut OK"

Write-Host "2. frozen server under scrubbed env (no system python reachable)..."
$srv = Invoke-Scrubbed "$App\kicad-mcp-server.exe" @("--version") 0
$ver = $srv.StandardOutput.ReadToEnd().Trim()  # read to EOF first, then wait (stream-flush rule)
$srv.WaitForExit()
if ($ver -notmatch "kicad-mcp-plugin") {
    $err = $srv.StandardError.ReadToEnd()
    throw "server --version unexpected: [$ver] exit=$($srv.ExitCode) stderr=[$err]"
}
Write-Host "   $ver"

$srv = Invoke-Scrubbed "$App\kicad-mcp-server.exe" @(
    "--transport","streamable-http","--host","127.0.0.1","--port","$Port") 12
try {
    $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"u4-local-smoke","version":"0"}}}'
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/mcp" -Method Post -Body $body `
        -ContentType "application/json" -Headers @{Accept="application/json, text/event-stream"} -TimeoutSec 15
    if ($r.StatusCode -ne 200) { throw "HTTP $($r.StatusCode)" }
    Write-Host "   MCP initialize over HTTP: 200"
} finally {
    if (-not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
}

Write-Host "3. launcher under scrubbed env (liveness + WebView2 tree)..."
$before = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue).Count
$lproc = Invoke-Scrubbed "$App\KiCad-MCP Launcher.exe" @() 20  # NOT $app — case-insensitive clash with $App
try {
    if ($lproc.HasExited) { throw "launcher exited (code $($lproc.ExitCode))" }
    $after = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue).Count
    if ($after -le $before) { throw "no new WebView2 renderer processes ($before -> $after)" }
    Write-Host "   launcher alive, WebView2 procs $before -> $after"
} finally {
    if (-not $lproc.HasExited) { Stop-Process -Id $lproc.Id -Force }
    Start-Sleep -Seconds 2
    Get-Process msedgewebview2 -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "*Programs\KiCad-MCP Launcher*" -or $_.MainWindowTitle -eq "" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

Write-Host "4. silent uninstall..."
$unins = Get-ChildItem "$App\unins*.exe" | Select-Object -First 1
if (-not $unins) { throw "uninstaller not found" }
$p = Start-Process -FilePath $unins.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "uninstaller exit code $($p.ExitCode)" }
Start-Sleep -Seconds 5
if (Test-Path "$App\KiCad-MCP Launcher.exe") { throw "app exe survived uninstall" }
if (Test-Path $Shortcut) { throw "shortcut survived uninstall" }
Write-Host "   uninstall clean"

Write-Host "LOCAL SMOKE PASS (scrubbed-env self-sufficiency + lifecycle)"
