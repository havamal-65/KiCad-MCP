# U4 sandbox smoke - INNER script (runs inside Windows Sandbox as LogonCommand).
# Mapped folders (set up by sandbox_smoke.ps1):
#   C:\smoke\artifact  (read-only)  - contains the Setup exe
#   C:\smoke\tools     (read-only)  - this script
#   C:\smoke\results   (writable)   - result.json + transcript land here
#
# Verifies REQ-U4-007 on a pristine Windows with no Python:
#   no python -> silent install -> dir + shortcut -> app launches (real
#   interactive desktop here, so the window truly opens) -> WebView2 tree
#   alive -> silent uninstall -> dir + shortcut gone.

$ErrorActionPreference = "Continue"
$Results = "C:\smoke\results"
$Log = Join-Path $Results "transcript.txt"
$Steps = [System.Collections.Generic.List[object]]::new()
$Failed = $false

function Step([string]$Name, [scriptblock]$Check) {
    $ok = $false
    $detail = ""
    try {
        $detail = & $Check
        $ok = $true
    } catch {
        $detail = $_.Exception.Message
        $script:Failed = $true
    }
    $script:Steps.Add(@{ step = $Name; ok = $ok; detail = "$detail" })
    Add-Content $Log "$(Get-Date -Format o)  [$(if ($ok) {'PASS'} else {'FAIL'})] $Name - $detail"
}

New-Item -ItemType Directory -Force $Results | Out-Null
Add-Content $Log "sandbox smoke started $(Get-Date -Format o)"

$App = "$env:LOCALAPPDATA\Programs\KiCad-MCP Launcher"
$Shortcut = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\KiCad-MCP Launcher.lnk"

Step "clean-machine premise: no python on PATH" {
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py -and $py.Source -notlike "*WindowsApps*") { throw "python found at $($py.Source)" }
    "no real python (store alias excluded)"
}

Step "silent install" {
    $setup = Get-ChildItem "C:\smoke\artifact\KiCad-MCP-Launcher-Setup-*.exe" | Select-Object -First 1
    if (-not $setup) { throw "setup exe not found in artifact folder" }
    $p = Start-Process -FilePath $setup.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "installer exit code $($p.ExitCode)" }
    "installed $($setup.Name)"
}

Step "install dir + start-menu shortcut exist" {
    if (-not (Test-Path "$App\KiCad-MCP Launcher.exe")) { throw "app exe missing" }
    if (-not (Test-Path "$App\kicad-mcp-server.exe")) { throw "server exe missing" }
    if (-not (Test-Path $Shortcut)) { throw "start-menu shortcut missing" }
    "all present"
}

Step "installer provisioned WebView2 runtime (QU4-4, zero manual steps)" {
    # A pristine sandbox has no Edge WebView2 runtime; the bundled bootstrapper in
    # the installer must have provisioned it during silent install. Probe the same
    # client GUID / pv value the app + installer.iss check.
    $guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid"
    )
    $pv = $null
    foreach ($k in $keys) {
        $v = (Get-ItemProperty -Path $k -Name pv -ErrorAction SilentlyContinue).pv
        if ($v -and $v -ne "0.0.0.0") { $pv = $v; break }
    }
    if (-not $pv) { throw "WebView2 runtime absent after install (installer did not provision it)" }
    "runtime pv=$pv"
}

Step "dashboard renders: WebView2 tree alive after 25s" {
    $p = Start-Process -FilePath "$App\KiCad-MCP Launcher.exe" -PassThru
    Start-Sleep -Seconds 25
    if ($p.HasExited) { throw "launcher exited (code $($p.ExitCode))" }
    $wv = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue)
    if ($wv.Count -lt 1) { throw "no WebView2 renderer processes even with runtime" }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Name "KiCad-MCP Launcher" -Force -ErrorAction SilentlyContinue
    Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    "dashboard rendered, $($wv.Count) WebView2 procs"
}

Step "frozen server answers --version" {
    $out = & "$App\kicad-mcp-server.exe" --version 2>&1
    if ("$out" -notmatch "kicad-mcp-plugin") { throw "unexpected: $out" }
    "$out"
}

Step "silent uninstall removes app dir + shortcut" {
    # Kill any launcher still holding its own exe (e.g. a WebView2-missing dialog).
    Stop-Process -Name "KiCad-MCP Launcher" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $unins = Get-ChildItem "$App\unins*.exe" | Select-Object -First 1
    if (-not $unins) { throw "uninstaller not found" }
    # Inno's uninstaller copies itself to %TEMP% and re-execs, returning before the
    # removal completes -- poll for the app exe to disappear (up to 40s).
    Start-Process -FilePath $unins.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART"
    $gone = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Test-Path "$App\KiCad-MCP Launcher.exe")) { $gone = $true; break }
    }
    if (-not $gone) { throw "app exe survived uninstall (40s)" }
    if (Test-Path $Shortcut) { throw "shortcut survived uninstall" }
    "clean"
}

@{
    verdict = if ($Failed) { "FAIL" } else { "PASS" }
    steps = $Steps
    finished = (Get-Date -Format o)
} | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Results "result.json") -Encoding UTF8
Add-Content $Log "sandbox smoke finished: $(if ($Failed) { 'FAIL' } else { 'PASS' })"
