<#
Build native/appbiz_adapter.cpp without a Visual Studio installation.

The adapter must be compiled with the MSVC toolchain (appbiz_agent.js hands the
module MSVC-layout std::string / std::unordered_map / std::function objects), so
a MinGW or clang build is not a substitute. When no Visual Studio is installed,
an equivalent toolchain can be assembled from the Visual Studio package cache
plus the Windows SDK:

  1. compiler     - %ProgramData%\Microsoft\VisualStudio\Packages\
                    Microsoft.VC.<ver>.Tools.HostX64.TargetX64.base\payload.vsix
  2. STL headers  - ...\Microsoft.VC.<ver>.CRT.Headers.base\payload.vsix
  3. CRT libraries- ...\Microsoft.VC.<ver>.CRT.x64.Desktop.base\payload.vsix
  4. compiler UI  - ...\Microsoft.VC.<ver>.Tools.HostX64.TargetX64.Res.base\payload.vsix
  5. UCRT headers - Win11SDK_10.0.26100\winsdksetup.exe /layout <dir>, then
                    msiexec /a "Installers\Universal CRT Headers Libraries and Sources-x86_en-us.msi" /qn TARGETDIR=<dir>
  6. import libs  - nuget package microsoft.windows.sdk.cpp.x64 (kernel32.lib and friends)

This script only consumes an already-prepared toolchain root; see the layout
expectations below. It does not download anything.

Usage:
    pwsh -File tools\build_appbiz_adapter_offline.ps1 -ToolchainRoot D:\toolchain
#>

param(
    [string]$ToolchainRoot = 'D:\temp\_vs',
    [string]$BridgeRoot = (Split-Path $PSScriptRoot -Parent),
    [string]$OutputName = 'appbiz_adapter.dll',
    [string]$MsvcVersion = '14.44.35207',
    [string]$SdkVersion = '10.0.26100.0'
)

$ErrorActionPreference = 'Stop'

$cl = Join-Path $ToolchainRoot "msvc\tools\Contents\VC\Tools\MSVC\$MsvcVersion\bin\Hostx64\x64\cl.exe"
$stl = Join-Path $ToolchainRoot "msvc\headers\Contents\VC\Tools\MSVC\$MsvcVersion\include"
$crtLib = Join-Path $ToolchainRoot "msvc\crt\Contents\VC\Tools\MSVC\$MsvcVersion\lib\x64"
$ucrtInclude = Join-Path $ToolchainRoot "ucrt\Windows Kits\10\Include\$SdkVersion\ucrt"
$ucrtLib = Join-Path $ToolchainRoot "ucrt\Windows Kits\10\Lib\$SdkVersion\ucrt\x64"
$umLib = Join-Path $ToolchainRoot "sdk\c\um\x64"
$source = Join-Path $BridgeRoot 'native\appbiz_adapter.cpp'
$output = Join-Path $BridgeRoot 'build'

foreach ($required in @($cl, $stl, $crtLib, $ucrtInclude, $ucrtLib, $umLib, $source)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "toolchain component missing: $required"
    }
}

New-Item -ItemType Directory -Force -Path $output | Out-Null
$outputDll = Join-Path $output $OutputName

# /MT keeps the adapter free of any msvcp140.dll / vcruntime140.dll dependency
# inside the Qianniu process, and APPBIZ_ADAPTER_NO_SDK_HEADERS swaps the
# Windows SDK header for the minimal declarations the file actually uses.
# OLDNAMES.lib only carries legacy symbol aliases this module never references.
$clArguments = @(
    '/nologo', '/std:c++17', '/EHsc', '/MT', '/O2',
    '/DAPPBIZ_ADAPTER_NO_SDK_HEADERS', '/LD',
    "/I$stl", "/I$ucrtInclude",
    "/Fo$output\", "/Fe$outputDll",
    $source,
    '/link', '/NODEFAULTLIB:OLDNAMES',
    "/LIBPATH:$crtLib", "/LIBPATH:$ucrtLib", "/LIBPATH:$umLib",
    'libcmt.lib', 'libcpmt.lib', 'libvcruntime.lib', 'libucrt.lib', 'kernel32.lib'
)

& $cl @clArguments
if ($LASTEXITCODE -ne 0) {
    throw "appbiz adapter build failed with exit code $LASTEXITCODE"
}

$built = Get-Item -LiteralPath $outputDll
Write-Output "built: $($built.FullName) ($($built.Length) bytes)"
Write-Output 'verify with: py -3.10 tools\verify_adapter_load.py'
