#Requires -Version 5.1
<#
  Build a portable Windows distribution for Odysseus.

  Output layout:
    dist\Odysseus\Odysseus.exe
    dist\Odysseus\static\...
    dist\Odysseus\scripts\...
    dist\Odysseus\mcp_servers\...
    dist\Odysseus\services\hwfit\data\...

  The app then keeps using its normal filesystem layout when frozen.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\build-windows-portable.ps1
#>

param([switch]$CheckPython)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    exit 1
}

Write-Step "Checking for Python"
. (Join-Path $PSScriptRoot "scripts\_lib\python-runtime.ps1")
try { $runtime = Resolve-OdysseusPython -RepoDir $PSScriptRoot -VenvNames @("venv", ".venv") }
catch { Fail $_.Exception.Message }
Write-Host $runtime.Status
if ($CheckPython) { exit 0 }
$pyExe = $runtime.Executable
$pyArgs = $runtime.Arguments
if (-not $runtime.Venv) {
    Write-Step "Creating virtual environment (venv)"
    & $pyExe @pyArgs -m venv venv
    if ($LASTEXITCODE -ne 0) { Fail "Failed to create the virtual environment." }
    $pyExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
}
& $pyExe (Join-Path $PSScriptRoot "src\python_runtime.py")
if ($LASTEXITCODE -ne 0) { Fail "The build environment does not use the supported Python." }

Write-Step "Installing build dependencies"
& $pyExe -c "import importlib.util; raise SystemExit(importlib.util.find_spec('pip') is None)"
if ($LASTEXITCODE -ne 0) {
    & $pyExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { Fail "Unable to bootstrap pip in the existing environment." }
}
& $pyExe -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
$installArgs = @()
& $pyExe -c "import importlib.metadata as m; raise SystemExit('chromadb-client' not in {d.metadata['Name'].lower() for d in m.distributions()})"
if ($LASTEXITCODE -eq 0) {
    & $pyExe -m pip uninstall -y chromadb-client
    if ($LASTEXITCODE -ne 0) { Fail "Unable to remove the old conflicting Chroma client." }
    $installArgs += "--force-reinstall"
}
& $pyExe -m pip install @installArgs --require-hashes -r requirements.lock -r requirements-build.lock
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed." }

Write-Step "Building portable exe bundle"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

$dataArgs = @(
    "--add-data", "static;static",
    "--add-data", "scripts;scripts",
    "--add-data", "mcp_servers;mcp_servers",
    "--add-data", "services/hwfit/data;services/hwfit/data",
    "--add-data", "config;config",
    "--add-data", ".env.example;.env.example",
    "--add-data", ".python-version;."
)

& $pyExe -m PyInstaller --noconfirm --clean --onedir --noconsole --icon=static/icon.ico --name Odysseus @dataArgs launcher.py
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed." }

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "Portable app folder: $PSScriptRoot\dist\Odysseus" -ForegroundColor Green
Write-Host "Distribute the whole folder (or zip it) so static assets and scripts stay with the exe." -ForegroundColor Green
