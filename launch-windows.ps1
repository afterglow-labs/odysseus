#Requires -Version 5.1
<#
  Odysseus - native Windows launcher (no Docker).

  One command to: create a virtualenv, install dependencies, run first-time
  setup (prints an admin password on first run), and start the server.
  Safe to re-run - it skips whatever already exists.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -Port 7000 -BindHost 127.0.0.1

  Tip: bind 127.0.0.1 (default) for local-only use. Use 0.0.0.0 only when you
  intentionally want other devices on your LAN to reach it.
#>
param(
    [int]$Port = 7000,
    [string]$BindHost = "127.0.0.1",
    [switch]$CheckPython
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Test-WindowsBashStub($path) {
    if (-not $path) { return $false }
    $lowered = $path.ToLowerInvariant()
    foreach ($stub in @("system32\bash.exe", "sysnative\bash.exe", "windowsapps\bash.exe")) {
        if ($lowered.Contains($stub)) { return $true }
    }
    return $false
}

function Find-GitBash {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd -and -not (Test-WindowsBashStub $cmd.Source)) { return $cmd.Source }

    $roots = @()
    foreach ($name in @("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LocalAppData")) {
        $base = [Environment]::GetEnvironmentVariable($name)
        if ($base) {
            $roots += (Join-Path $base "Git")
            if ($name -eq "LocalAppData") { $roots += (Join-Path $base "Programs\Git") }
        }
    }
    $roots += @("C:\Program Files\Git", "C:\Program Files (x86)\Git")

    foreach ($root in ($roots | Select-Object -Unique)) {
        foreach ($relative in @("bin\bash.exe", "usr\bin\bash.exe")) {
            $candidate = Join-Path $root $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

# 1. Select only the supported Python, including an existing environment.
Write-Step "Checking for Python"
. (Join-Path $PSScriptRoot "scripts\_lib\python-runtime.ps1")
try { $runtime = Resolve-OdysseusPython -RepoDir $PSScriptRoot }
catch { Fail $_.Exception.Message }
Write-Host $runtime.Status
if ($CheckPython) { exit 0 }
$pyExe = $runtime.Executable
$pyArgs = $runtime.Arguments

# 2. Create the virtualenv if missing; incompatible environments fail above.
$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not $runtime.Venv) {
    Write-Step "Creating virtual environment (venv)"
    & $pyExe @pyArgs -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Failed to create the virtual environment." }
}
& $venvPy (Join-Path $PSScriptRoot "src\python_runtime.py")
if ($LASTEXITCODE -ne 0) { Fail "The virtual environment does not use the supported Python." }

# 3. Install / update dependencies
Write-Step "Installing dependencies (first run can take a few minutes)"
& $venvPy -c "import importlib.util; raise SystemExit(importlib.util.find_spec('pip') is None)"
if ($LASTEXITCODE -ne 0) {
    & $venvPy -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { Fail "Unable to bootstrap pip in the existing environment." }
}
& $venvPy -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
$installArgs = @()
& $venvPy -c "import importlib.metadata as m; raise SystemExit('chromadb-client' not in {d.metadata['Name'].lower() for d in m.distributions()})"
if ($LASTEXITCODE -eq 0) {
    & $venvPy -m pip uninstall -y chromadb-client
    if ($LASTEXITCODE -ne 0) { Fail "Unable to remove the old conflicting Chroma client." }
    $installArgs += "--force-reinstall"
}
& $venvPy -m pip install @installArgs --require-hashes -r requirements.lock
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. Scroll up for the pip error." }

# 4. First-time setup (creates data dirs, DB, .env, admin user)
Write-Step "Running first-time setup"
& $venvPy setup.py
if ($LASTEXITCODE -ne 0) { Fail "setup.py failed." }

# 5. Friendly note about Git Bash (full Cookbook / agent-shell parity)
if (-not (Find-GitBash)) {
    Write-Host ""
    Write-Host "NOTE: Git Bash (bash.exe) was not found on PATH." -ForegroundColor Yellow
    Write-Host "      The core app works without it. For full Cookbook background" -ForegroundColor Yellow
    Write-Host "      downloads and the agent shell tool, install Git for Windows:" -ForegroundColor Yellow
    Write-Host "      https://git-scm.com/download/win" -ForegroundColor Yellow
}

# 6. Point CUDA_PATH at a real CUDA toolkit so GPU llama-cpp-python can import.
$cudaBase = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
if (Test-Path $cudaBase) {
    $cudaBest = Get-ChildItem $cudaBase -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "bin") } |
        Sort-Object { try { [version]($_.Name -replace "^v", "") } catch { [version]"0.0" } } -Descending |
        Select-Object -First 1
    if ($cudaBest) {
        $env:CUDA_PATH = $cudaBest.FullName
        Write-Host ("Using CUDA_PATH = " + $cudaBest.FullName) -ForegroundColor Cyan
    }
}

# 7. Start the server (use `python -m uvicorn` - bare `uvicorn` may not be on PATH)
# -Port only reaches uvicorn as a flag. Everything that builds a URL for this
# instance - internal_api_base(), companion pairing, the MCP OAuth callback -
# reads APP_PORT, so set it too or they all assume 7000.
$env:APP_PORT = $Port
Write-Step ("Starting Odysseus at http://{0}:{1}" -f $BindHost, $Port)
Write-Host "Press Ctrl+C to stop."
Write-Host ""
& $venvPy -m uvicorn app:app --host $BindHost --port $Port
