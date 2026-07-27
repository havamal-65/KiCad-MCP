# U4 one-command build (REQ-U4-001): clean checkout -> frozen app -> installer.
#
#   pwsh -File installer\build.ps1                # unsigned (default, REQ-U4-009)
#   pwsh -File installer\build.ps1 -CertPath p.pfx -CertPassword ***   # signed
#
# Output: installer\out\KiCad-MCP-Launcher-Setup-<version>.exe

param(
    [string]$CertPath = "",
    [string]$CertPassword = "",
    [switch]$SkipInstaller  # freeze only (used by the frozen-app smoke)
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Repo ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "venv python not found at $Python — create the dev venv first (pip install -e .[launcher,dev,packaging])"
}

# 1. PyInstaller present?
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "installing packaging tooling (pyinstaller) into the venv..."
    & $Python -m pip install "pyinstaller>=6" | Out-Null
}

# 2. Version from pyproject (single source of truth).
$Version = & $Python -c "import tomllib,pathlib;print(tomllib.load(open(pathlib.Path(r'$Repo')/'pyproject.toml','rb'))['project']['version'])"
if (-not $Version) { throw "could not read project version from pyproject.toml" }
Write-Host "building KiCad-MCP Launcher $Version"

# 3. Freeze (two exes, one shared _internal).
& $Python -m PyInstaller --noconfirm `
    --distpath (Join-Path $PSScriptRoot "dist") `
    --workpath (Join-Path $PSScriptRoot "build") `
    (Join-Path $PSScriptRoot "kicad_mcp_bundle.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$DistDir = Join-Path $PSScriptRoot "dist\KiCad-MCP Launcher"
foreach ($exe in @("KiCad-MCP Launcher.exe", "kicad-mcp-server.exe")) {
    if (-not (Test-Path (Join-Path $DistDir $exe))) { throw "missing frozen exe: $exe" }
}

# 4. Optional signing of the frozen exes (REQ-U4-009: zero rework when a cert appears).
function Sign-File([string]$Path) {
    & signtool sign /f $CertPath /p $CertPassword /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $Path
    if ($LASTEXITCODE -ne 0) { throw "signtool failed for $Path" }
}
if ($CertPath) {
    foreach ($exe in @("KiCad-MCP Launcher.exe", "kicad-mcp-server.exe")) {
        Sign-File (Join-Path $DistDir $exe)
    }
}

if ($SkipInstaller) {
    Write-Host "freeze complete (installer skipped): $DistDir"
    exit 0
}

# 5. Inno Setup compiler (winget-install headlessly if absent).
$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc) {
    Write-Host "installing Inno Setup via winget..."
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $Iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup (ISCC.exe) not found and winget install failed" }

# 6. Compile the installer.
& $Iscc (Join-Path $PSScriptRoot "installer.iss") "/DAppVersion=$Version" "/DDistDir=$DistDir"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$Artifact = Join-Path $PSScriptRoot "out\KiCad-MCP-Launcher-Setup-$Version.exe"
if (-not (Test-Path $Artifact)) { throw "expected artifact missing: $Artifact" }

# 7. Optional signing of the installer itself.
if ($CertPath) { Sign-File $Artifact }

$SizeMb = [Math]::Round((Get-Item $Artifact).Length / 1MB, 1)
Write-Host "DONE: $Artifact ($SizeMb MB$(if ($CertPath) { ', signed' } else { ', unsigned' }))"
