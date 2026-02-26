<#
.SYNOPSIS
    Run kicad-mcp inside an isolated virtual environment.

.DESCRIPTION
    On first run, creates .venv at the project root and installs kicad-mcp
    into it so it never touches the global Python environment.
    On subsequent runs, the venv is reused and startup is immediate.
    All arguments are forwarded to kicad-mcp.

.EXAMPLE
    .\run.ps1
    .\run.ps1 --transport sse --port 8765
    .\run.ps1 --check
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PassThruArgs
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Ensure-Venv {
    if (-not (Test-Path $VenvPython)) {
        Write-Host "[kicad-mcp] Creating virtual environment at $VenvDir ..." -ForegroundColor Cyan
        python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
    }

    # Check whether the package is importable inside the venv
    & $VenvPython -c "import kicad_mcp" *>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[kicad-mcp] Installing kicad-mcp into venv..." -ForegroundColor Cyan
        & $VenvPython -m pip install -e $ProjectDir --quiet
        if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
    }
}

Ensure-Venv

& $VenvPython -m kicad_mcp @PassThruArgs
exit $LASTEXITCODE
