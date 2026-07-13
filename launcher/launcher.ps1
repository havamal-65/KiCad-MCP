# Launch the KiCad-MCP Stack Launcher window (no console box).
# Runs under the PROJECT VENV so launcher.* and kicad_mcp.* import; uses
# pythonw.exe so the Tk window has no console window behind it.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

# Prefer venv pythonw (has kicad_mcp + psutil + tkinter), then venv python, then system.
$candidates = @(
    (Join-Path $repo '.venv\Scripts\pythonw.exe'),
    (Join-Path $repo '.venv\Scripts\python.exe'),
    'C:\Python312\pythonw.exe'
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { $py = (Get-Command python.exe).Source }

# Run as a module from the repo root so the top-level `launcher` package resolves.
Start-Process -FilePath $py -ArgumentList '-m', 'launcher' -WorkingDirectory $repo
Write-Host "Launched KiCad-MCP Launcher ($py)."
