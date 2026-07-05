# Launch the KiCad MCP Health Monitor window (no console box).
# Runs under the PROJECT VENV so the authoritative run_startup_checklist() imports;
# uses pythonw.exe so the GUI has no console window behind it.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$script = Join-Path $PSScriptRoot 'mcp_health_monitor.py'

# Prefer venv pythonw (has kicad_mcp + psutil + tkinter), then venv python, then system.
$candidates = @(
    (Join-Path $repo '.venv\Scripts\pythonw.exe'),
    (Join-Path $repo '.venv\Scripts\python.exe'),
    'C:\Python312\pythonw.exe'
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { $py = (Get-Command python.exe).Source }

Start-Process -FilePath $py -ArgumentList "`"$script`""
Write-Host "Launched KiCad MCP Health Monitor ($py)."
