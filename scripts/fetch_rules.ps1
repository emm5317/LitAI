# Download the official rule texts listed in lpc\rules\sources.json into rules\sources\.
# Writes rules\sources\manifest.lock.json with each file's SHA-256 and download date,
# so you can tell later whether a source changed.
#
# Usage (from the repo root, Windows PowerShell 5 or PowerShell 7):
#   .\scripts\fetch_rules.ps1
#
# Sources with no direct download link are listed at the end; save those by hand
# from their landing pages into rules\sources\.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $root "lpc\rules\sources.json"
$outDir = Join-Path $root "rules\sources"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$lock = @()
$manual = @()

foreach ($src in $manifest.sources) {
    if (-not $src.download) {
        $manual += $src
        continue
    }
    $ext = if ($src.download -match '\.pdf$') { ".pdf" } else { ".html" }
    $dest = Join-Path $outDir ($src.id + $ext)
    Write-Host "Downloading $($src.id) ..."
    try {
        Invoke-WebRequest -Uri $src.download -OutFile $dest -UseBasicParsing -UserAgent "LawPracticeConsole/0.1"
        $hash = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
        $lock += [pscustomobject]@{ id = $src.id; file = (Split-Path $dest -Leaf); sha256 = $hash;
                                    url = $src.download; downloaded = (Get-Date -Format "yyyy-MM-dd") }
    } catch {
        Write-Warning "Failed: $($src.id) - $($_.Exception.Message)"
        $manual += $src
    }
}

$lock | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $outDir "manifest.lock.json")

Write-Host ""
Write-Host "Save these by hand from their landing pages:"
foreach ($m in $manual) { Write-Host ("  {0,-24} {1}" -f $m.id, $m.landing) }
