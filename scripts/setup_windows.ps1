# One-time setup on Windows + WSL2.
#   1. builds the collector and scenarios and installs the backend inside WSL (scripts/setup_linux.sh)
#   2. installs and builds the frontend with Windows Node.js
# Usage: .\scripts\setup_windows.ps1 [-Distro Ubuntu]
param([string]$Distro = "Ubuntu")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$linuxRoot = (wsl.exe -d $Distro --exec wslpath -a ($root -replace '\\', '/')).Trim()

Write-Host "==> Linux part (WSL distro '$Distro')"
wsl.exe -d $Distro --cd $linuxRoot --exec bash scripts/setup_linux.sh
if ($LASTEXITCODE -ne 0) { throw "Linux setup failed" }

Write-Host "==> Frontend (Node $(node --version))"
Push-Location "$root\frontend"
npm ci
npm run build
Pop-Location
Write-Host "Done. Start with: .\scripts\start_windows.ps1"
