param(
    [string]$Config = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$Foreground
)

$python = (Get-Command py.exe -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot 'standalone_bridge.py'
$pidFile = Join-Path $PSScriptRoot 'state\standalone_bridge.pid'

$bridgeRunning = $false
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = [int](Get-Content -Raw -LiteralPath $pidFile)
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        Write-Output "ALREADY_RUNNING:$oldPid"
        $bridgeRunning = $true
    }
}

if (-not $bridgeRunning) {
    $injector = Join-Path $PSScriptRoot 'tools\inject_runtime_webui.py'
    & $python -3.10 $injector (Join-Path $PSScriptRoot 'runtime') $Config (Join-Path $PSScriptRoot 'browser_bridge.js')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the standalone browser bridge into the packaged runtime.'
    }

    if ($Foreground) {
        & $python -3.10 $script --config $Config
        exit $LASTEXITCODE
    }

    $process = Start-Process -FilePath $python -ArgumentList @('-3.10', $script, '--config', $Config) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pidFile) | Out-Null
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ASCII
    Write-Output "STARTED:$($process.Id)"
}

$configData = Get-Content -Raw -LiteralPath $Config | ConvertFrom-Json
if ($configData.dock_enabled) {
    $dockScript = Join-Path $PSScriptRoot 'docked_workbench.py'
    $dockPidFile = Join-Path $PSScriptRoot 'state\docked_workbench.pid'
    $dockRunning = $false
    if (Test-Path -LiteralPath $dockPidFile) {
        $oldDockPid = [int](Get-Content -Raw -LiteralPath $dockPidFile)
        if (Get-Process -Id $oldDockPid -ErrorAction SilentlyContinue) {
            Write-Output "DOCK_ALREADY_RUNNING:$oldDockPid"
            $dockRunning = $true
        }
    }
    if (-not $dockRunning) {
        $dockProcess = Start-Process -FilePath $python -ArgumentList @('-3.10', $dockScript, '--config', $Config) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath $dockPidFile -Value $dockProcess.Id -Encoding ASCII
        Write-Output "DOCK_STARTED:$($dockProcess.Id)"
    }
}
