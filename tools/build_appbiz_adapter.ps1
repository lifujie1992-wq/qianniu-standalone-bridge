param(
    [string]$Configuration = 'Release',
    [string]$OutputName = 'appbiz_adapter.dll'
)

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) {
    throw 'vswhere.exe was not found.'
}

$installation = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installation) {
    throw 'Visual C++ x64 build tools were not found.'
}

$vcvars = Join-Path $installation 'VC\Auxiliary\Build\vcvars64.bat'
$source = Join-Path $PSScriptRoot '..\native\appbiz_adapter.cpp'
$output = Join-Path $PSScriptRoot '..\build'
$outputDll = Join-Path $output $OutputName
$outputPdb = [System.IO.Path]::ChangeExtension($outputDll, '.pdb')
New-Item -ItemType Directory -Force -Path $output | Out-Null

$optimization = if ($Configuration -eq 'Debug') { '/Od /Zi' } else { '/O2' }
$command = 'call "{0}" && cl.exe /nologo /std:c++17 /EHsc /MD {1} /LD "{2}" /link /OUT:"{3}" /PDB:"{4}"' -f `
    $vcvars,
    $optimization,
    $source,
    $outputDll,
    $outputPdb

& $env:ComSpec /d /s /c $command
if ($LASTEXITCODE -ne 0) {
    throw "appbiz adapter build failed with exit code $LASTEXITCODE"
}

Get-Item -LiteralPath $outputDll |
    Select-Object FullName, Length, LastWriteTime
