# Build Otlet for Windows: onedir app + zip archive.
# Run on a Windows machine with uv installed (https://docs.astral.sh/uv/).
# PyInstaller cannot cross-compile, so the Windows build runs on Windows.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Version = (Get-Content pyproject.toml |
    Select-String '^version = "(.+)"').Matches[0].Groups[1].Value
Write-Host "==> Building Otlet v$Version (win64)..."

uv sync
uv run pyinstaller otlet.spec --clean --noconfirm

if (-Not (Test-Path "dist\otlet\otlet.exe")) {
    throw "build failed: dist\otlet\otlet.exe not found"
}

$Zip = "dist\Otlet-$Version-win64.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path dist\otlet -DestinationPath $Zip
Write-Host "==> Done: $Zip"
Write-Host "    App entry: dist\otlet\otlet.exe"
Write-Host "    Note: the GUI needs the Microsoft WebView2 Runtime,"
Write-Host "    preinstalled on Windows 10/11 (Edge)."
