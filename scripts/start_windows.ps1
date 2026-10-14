# Start ThreadLens on Windows: backend inside WSL2 (Linux), UI in your browser.
# Usage (PowerShell, from the project root):  .\scripts\start_windows.ps1 [-Distro Ubuntu] [-Dev]
#   -Dev   also start the Vite dev server on http://localhost:5173 (hot reload)
param(
    [string]$Distro = "Ubuntu",
    [switch]$Dev
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$linuxRoot = (wsl.exe -d $Distro --exec wslpath -a ($root -replace '\\', '/')).Trim()
Write-Host "ThreadLens root in WSL: $linuxRoot"

Start-Process wsl.exe -ArgumentList @("-d", $Distro, "--cd", $linuxRoot, "--exec", "bash", "scripts/run_backend.sh")
Write-Host "Backend starting in a new window (http://localhost:8000)..."

if ($Dev) {
    Start-Process powershell.exe -ArgumentList @("-NoExit", "-Command", "cd '$root\frontend'; npm run dev")
    Start-Sleep -Seconds 4
    Start-Process "http://localhost:5173"
} else {
    if (-not (Test-Path "$root\frontend\dist\index.html")) {
        Write-Host "Building the frontend (first run)..."
        Push-Location "$root\frontend"; npm run build; Pop-Location
    }
    Start-Sleep -Seconds 3
    Start-Process "http://localhost:8000"
}
