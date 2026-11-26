# One-time setup on Windows. Run from the repo root in PowerShell:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if scripts are blocked
#   .\scripts\setup.ps1
#
# Needs Python 3.11+ from python.org (tick "Add python.exe to PATH") or `winget install Python.Python.3.12`.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[desktop,dev]"

Write-Host ""
Write-Host "Setup complete."
Write-Host "  Run the app:     .\scripts\dev.ps1"
Write-Host "  Run the tests:   .\.venv\Scripts\python.exe -m pytest"
Write-Host "  Save API key:    .\.venv\Scripts\python.exe -c ""import keyring, getpass; keyring.set_password('LawPracticeConsole','anthropic', getpass.getpass('Anthropic API key: '))"""
