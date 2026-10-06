param(
    [string]$PythonExe = 'python',
    [string]$AdapterPath = ''
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows x64.' }
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed: $LASTEXITCODE" }
}
Invoke-Checked -Program $PythonExe -Arguments @('-c', 'import struct,sys;assert struct.calcsize("P")==8,"Use x64 Python";assert sys.version_info>=(3,10),"Use Python 3.10+"')
$venv = Join-Path $root '.venv-windows-build'
if (-not (Test-Path -LiteralPath $venv)) {
    Invoke-Checked -Program $PythonExe -Arguments @('-m', 'venv', $venv)
}
$python = Join-Path $venv 'Scripts\python.exe'
Invoke-Checked -Program $python -Arguments @('-m', 'pip', 'install', '-r', 'requirements.txt', 'pyinstaller==6.16.0')
$version = (& $python -c 'import app_version;print(app_version.VERSION)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid app version.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$output = Join-Path $root "delivery\windows-$version-$stamp"
$package = Join-Path $output 'QianniuAIService'
New-Item -ItemType Directory -Path $package | Out-Null
if (-not $AdapterPath) {
    $AdapterPath = Join-Path $root 'build\appbiz_adapter.dll'
    if (-not (Test-Path -LiteralPath $AdapterPath)) {
        & (Join-Path $PSScriptRoot 'build_appbiz_adapter.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Native adapter build failed.' }
    }
}
if (-not (Test-Path -LiteralPath $AdapterPath)) { throw "Missing adapter: $AdapterPath" }
Invoke-Checked -Program $python -Arguments @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
    '--name', 'QianniuAgent', '--distpath', $package,
    '--workpath', (Join-Path $output 'work'), '--specpath', $output,
    '--hidden-import', 'taobao_message_contract', '--hidden-import', 'tmall_delivery_guard',
    '--collect-all', 'frida', '--collect-all', 'websockets', 'qianniu_app.py')
foreach ($name in @('browser_bridge.js', 'appbiz_agent.js', 'native_agent.js', 'workbench.html', 'config.example.json')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $package
}
New-Item -ItemType Directory -Path (Join-Path $package 'build') | Out-Null
Copy-Item -LiteralPath $AdapterPath -Destination (Join-Path $package 'build\appbiz_adapter.dll')
$exe = Join-Path $package 'QianniuAgent.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'EXE was not produced.' }
$commit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to record source commit.' }
@{ version=$version; commit=$commit; exe_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash } |
    ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $package 'build-info.json')
$zip = Join-Path $output "QianniuAIService-$version-windows-x64.zip"
Compress-Archive -Path (Join-Path $package '*') -DestinationPath $zip
Get-FileHash -Algorithm SHA256 -LiteralPath $zip
Write-Output "Package: $zip"
Write-Output 'Upgrade files only: close the old assistant, retain its config.json and runtime folder, then copy package contents into that installation. Validate sending on Windows before distributing.'
