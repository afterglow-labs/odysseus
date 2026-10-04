#Requires -Version 5.1
<#
Build the Windows desktop launcher, matching build-macos-app.sh.
The app runs from this checkout's isolated venv; only the small launcher is
frozen. Rebuild after moving the checkout. Profiles live in ~/.odysseus/data.
#>
param([string]$OutputDirectory = "dist\Odysseus-desktop")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot "scripts\_lib\python-runtime.ps1")
$runtime = Resolve-OdysseusPython -RepoDir $PSScriptRoot
if (-not $runtime.Venv) {
    throw "Set up Odysseus's isolated venv with launch-windows.ps1 before building."
}
$pyExe = $runtime.Executable
$iconPath = Join-Path $PSScriptRoot "static\icon.ico"
& $pyExe -m PyInstaller --noconfirm --clean --onefile --windowed --name Odysseus `
    --icon $iconPath --distpath $OutputDirectory `
    --workpath build\windows-desktop --specpath build\windows-desktop src\windows_desktop.py
if ($LASTEXITCODE -ne 0) { throw "Windows desktop launcher build failed." }
$output = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $OutputDirectory))
$config = @{ repo_dir = $PSScriptRoot } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $output "Odysseus-launcher.json"), $config, [Text.UTF8Encoding]::new($false))
@"
Odysseus Windows desktop launcher

Run Odysseus.exe. Keep Odysseus-launcher.json beside it.
This launcher uses the private Python environment at:
$PSScriptRoot\venv

The checkout and its venv must remain at that location, just like the Mac
launcher. Rebuild with build-windows-app.ps1 after moving the checkout.
Cookbook's local packages are installed into that same venv.
Your profile remains in %USERPROFILE%\.odysseus\data.
"@ | Set-Content -LiteralPath (Join-Path $output "README-Windows.txt") -Encoding UTF8
Write-Host "Desktop app: $output\Odysseus.exe"
