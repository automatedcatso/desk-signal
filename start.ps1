$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonPath = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

Set-Location -LiteralPath $ProjectRoot
Write-Host "Signal Desk: http://127.0.0.1:5000"
& $PythonPath run.py
