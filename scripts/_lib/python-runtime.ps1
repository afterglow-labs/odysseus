# Shared, read-only Python selection for the native Windows launch/build paths.
function Resolve-OdysseusPython {
    param([string]$RepoDir, [string[]]$VenvNames = @("venv"))

    $version = (Get-Content -Raw (Join-Path $RepoDir ".python-version")).Trim()
    $checker = Join-Path $RepoDir "src\python_runtime.py"

    foreach ($name in $VenvNames) {
        $directory = Join-Path $RepoDir $name
        if (-not (Test-Path $directory)) { continue }
        $executable = Join-Path $directory "Scripts\python.exe"
        $status = "Python executable is missing."
        $valid = $false
        if (Test-Path $executable) {
            try {
                $status = & $executable $checker 2>&1
                $valid = $LASTEXITCODE -eq 0
            } catch { $status = $_.Exception.Message }
        }
        if (-not $valid) {
            throw "Existing $name is incomplete or incompatible. $status Move it aside (Rename-Item $name $name.previous) and rerun this script to create CPython $version."
        }
        return [PSCustomObject]@{ Executable = $executable; Arguments = @(); Venv = $directory; Status = $status }
    }

    $candidates = @(
        @{ Command = "py"; Arguments = @("-$version") },
        @{ Command = "python$version"; Arguments = @() },
        @{ Command = "python"; Arguments = @() },
        @{ Command = "python3"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $command -or $command.Source -like "*WindowsApps*python*.exe") { continue }
        $executable = $command.Source
        $arguments = $candidate.Arguments
        # Python's current Windows install manager auto-installs missing versions
        # by default. Interpreter discovery (especially -CheckPython) is read-only.
        $automaticInstall = $env:PYTHON_MANAGER_AUTOMATIC_INSTALL
        $allowInstall = $env:PYLAUNCHER_ALLOW_INSTALL
        $alwaysInstall = $env:PYLAUNCHER_ALWAYS_INSTALL
        try {
            $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
            Remove-Item Env:PYLAUNCHER_ALLOW_INSTALL -ErrorAction SilentlyContinue
            Remove-Item Env:PYLAUNCHER_ALWAYS_INSTALL -ErrorAction SilentlyContinue
            $status = & $executable @arguments $checker 2>&1
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{ Executable = $executable; Arguments = $arguments; Venv = $null; Status = $status }
            }
        } catch { continue }
        finally {
            $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = $automaticInstall
            $env:PYLAUNCHER_ALLOW_INSTALL = $allowInstall
            $env:PYLAUNCHER_ALWAYS_INSTALL = $alwaysInstall
        }
    }
    throw "Install stable CPython $version (standard GIL build) from https://www.python.org/downloads/ and rerun this script. The Python launcher should support: py -$version"
}
