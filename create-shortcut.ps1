$ErrorActionPreference = "Stop"
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython))
{
    throw 'Run setup.ps1 first.'
}
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $taskShell.CreateShortcut((Join-Path $PSScriptRoot 'GSM7-20H10.lnk'))
$taskShortcut.TargetPath = $taskPython
$taskShortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'app.py') + '"'
$taskShortcut.WorkingDirectory = $PSScriptRoot
$taskShortcut.IconLocation = (Join-Path $PSScriptRoot 'assets\gsmbench.ico') + ',0'
$taskShortcut.Description = 'GSM7-20H10 Semiconductor Testbench'
$taskShortcut.Save()
