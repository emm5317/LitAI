# Build a Windows app folder with PyInstaller: dist\LawPracticeConsole\LawPracticeConsole.exe
# Run after setup.ps1. Code signing comes later (signtool + a code-signing certificate),
# so Windows SmartScreen will warn on first launch until the build is signed.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed --name LawPracticeConsole `
    --add-data "lpc\templates;lpc\templates" `
    --add-data "lpc\static;lpc\static" `
    --add-data "lpc\migrations;lpc\migrations" `
    --add-data "lpc\rules\data;lpc\rules\data" `
    --add-data "lpc\rules\sources.json;lpc\rules" `
    --collect-submodules uvicorn `
    --collect-data extract_msg `
    run_console.py

Write-Host "Built dist\LawPracticeConsole\LawPracticeConsole.exe"
