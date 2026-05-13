# MagiTrader — install Node + Python dependencies (Windows).
# Run from repo root:  .\scripts\setup.ps1
# Or:  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

Write-Host "MagiTrader setup — repo: $RepoRoot" -ForegroundColor Cyan

foreach ($tool in @("node", "npm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Error "$tool not found on PATH. Install Node.js LTS from https://nodejs.org/"
    }
}

Write-Host "`n[1/3] npm ci (repo root)…" -ForegroundColor Yellow
npm ci

Write-Host "`n[2/3] npm ci (frontend, legacy-peer-deps)…" -ForegroundColor Yellow
npm ci --prefix frontend --legacy-peer-deps

Write-Host "`n[3/3] pip install backend/requirements.txt…" -ForegroundColor Yellow
$req = Join-Path $RepoRoot "backend\requirements.txt"
$pipOk = $false

if (Get-Command python -ErrorAction SilentlyContinue) {
    python -m pip install -r $req
    if ($LASTEXITCODE -eq 0) { $pipOk = $true }
}

if (-not $pipOk -and (Get-Command py -ErrorAction SilentlyContinue)) {
    py -3 -m pip install -r $req
    if ($LASTEXITCODE -eq 0) { $pipOk = $true }
}

if (-not $pipOk) {
    Write-Error 'pip failed. Install Python 3.12+ from https://www.python.org/ and ensure "python" or "py -3" runs pip.'
}

Write-Host "`nDone. Copy .env if needed, then: npm run dev" -ForegroundColor Green
