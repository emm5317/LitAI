# Run the console from source. Pass -Browser to skip the desktop window and use your browser.
param([switch]$Browser)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if ($Browser) {
    & .\.venv\Scripts\python.exe -m lpc --server-only --port 8765
} else {
    & .\.venv\Scripts\python.exe -m lpc
}
