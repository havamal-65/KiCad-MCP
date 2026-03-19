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

function Write-Stderr {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Reset-PythonEnvironment {
    foreach ($Name in @("PYTHONHOME", "PYTHONPATH")) {
        Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
    }

    if (-not $env:FASTMCP_LOG_ENABLED) {
        $env:FASTMCP_LOG_ENABLED = "false"
    }
}

function Ensure-Venv {
    if (-not (Test-Path $VenvPython)) {
        Write-Stderr "[kicad-mcp] Creating virtual environment at $VenvDir ..."
        python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
    }

    # Check whether the package is importable inside the venv
    & $VenvPython -c "import kicad_mcp" *>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Stderr "[kicad-mcp] Installing kicad-mcp into venv..."
        & $VenvPython -m pip install -e $ProjectDir --quiet
        if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
    }
}

Reset-PythonEnvironment
Ensure-Venv

& $VenvPython -m kicad_mcp @PassThruArgs
exit $LASTEXITCODE
