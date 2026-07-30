# U4 clean-machine smoke (REQ-U4-007) - HOST script.
#
#   pwsh -File installer\sandbox_smoke.ps1
#
# Boots Windows Sandbox (a pristine Windows with no Python), silently installs
# the built artifact, verifies launch + WebView2 + uninstall, and reads the
# verdict back through a mapped results folder. Fully scripted - no manual
# steps (AR-U4-5). Requires the Windows Sandbox optional feature.

param(
    [int]$TimeoutMinutes = 10
)

$ErrorActionPreference = "Stop"
$Out = Join-Path $PSScriptRoot "out"
$Sandbox = "$env:WINDIR\System32\WindowsSandbox.exe"

if (-not (Test-Path $Sandbox)) {
    throw ("Windows Sandbox is not enabled. Enable it once (admin): " +
           "Enable-WindowsOptionalFeature -Online -FeatureName 'Containers-DisposableClientVM' -All " +
           "(then reboot) and re-run this script.")
}
$Artifact = Get-ChildItem (Join-Path $Out "KiCad-MCP-Launcher-Setup-*.exe") |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Artifact) { throw "no installer artifact in $Out - run installer\build.ps1 first" }

# Fresh results dir per run.
$Results = Join-Path $PSScriptRoot "sandbox_results"
if (Test-Path $Results) { Remove-Item $Results -Recurse -Force }
New-Item -ItemType Directory $Results | Out-Null

# The .wsb profile: artifact + tools read-only, results writable.
$Wsb = Join-Path $Results "smoke.wsb"
@"
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$Out</HostFolder>
      <SandboxFolder>C:\smoke\artifact</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$PSScriptRoot</HostFolder>
      <SandboxFolder>C:\smoke\tools</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$Results</HostFolder>
      <SandboxFolder>C:\smoke\results</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -ExecutionPolicy Bypass -Command "Start-Sleep 12; Copy-Item C:\smoke\tools\sandbox_inner.ps1 C:\inner.ps1 -Force; powershell.exe -ExecutionPolicy Bypass -File C:\inner.ps1 *&gt; C:\smoke\results\_inner.log"</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content $Wsb -Encoding UTF8

Write-Host "launching Windows Sandbox against $($Artifact.Name)..."
$proc = Start-Process -FilePath $Sandbox -ArgumentList $Wsb -PassThru

# Poll for the verdict.
$resultFile = Join-Path $Results "result.json"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while ((Get-Date) -lt $deadline -and -not (Test-Path $resultFile)) {
    Start-Sleep -Seconds 10
}

# Tear the sandbox down regardless.
Get-Process WindowsSandbox* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process WindowsSandboxClient -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $resultFile)) {
    throw "sandbox smoke produced no result within $TimeoutMinutes min (see $Results)"
}
$result = Get-Content $resultFile -Raw | ConvertFrom-Json
Write-Host "--- sandbox transcript:"
Get-Content (Join-Path $Results "transcript.txt") | ForEach-Object { Write-Host "  $_" }
Write-Host "--- verdict: $($result.verdict)"
if ($result.verdict -ne "PASS") { throw "sandbox smoke FAILED" }
Write-Host "SANDBOX SMOKE PASS"
