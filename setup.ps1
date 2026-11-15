param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0)
{
    throw "Failed to create Python environment"
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0)
{
    throw "Failed to install dependencies"
}
Write-Host "Ready. Run start.cmd"
& (Join-Path $PSScriptRoot 'create-shortcut.ps1')
