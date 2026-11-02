# Installs Hermes Lite on Windows into its own virtualenv and puts a `hermes-lite` command on PATH.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $env:LOCALAPPDATA 'hermes-lite'
$venv = Join-Path $data 'venv'

if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $venv }
    else { throw 'Python 3.11+ is needed: winget install Python.Python.3.12' }
}
$vpy = Join-Path $venv 'Scripts\python.exe'
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -e $here

# A small launcher in a folder that is on PATH by default for every user.
$bin = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
$exe = Join-Path $venv 'Scripts\hermes-lite.exe'
Set-Content -Path (Join-Path $bin 'hermes-lite.cmd') -Value "@echo off`r`n`"$exe`" %*" -Encoding ASCII

# Create ~/.hermes-lite/config.yaml on first install, and keep a copy of this version for --rollback.
& $exe --print-server-cmd | Out-Null
& $exe --snapshot

Write-Host 'Installed. Edit %USERPROFILE%\.hermes-lite\config.yaml (server.model_path, server.binary: llama-server.exe), then run: hermes-lite'
if (-not (Get-Command rg -ErrorAction SilentlyContinue)) { Write-Host 'Tip: install ripgrep for faster search_files (winget install BurntSushi.ripgrep.MSVC).' }
