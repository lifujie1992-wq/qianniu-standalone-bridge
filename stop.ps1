$pidFile = Join-Path $PSScriptRoot 'state\standalone_bridge.pid'
$python = (Get-Command py.exe -ErrorAction Stop).Source
$dockScript = Join-Path $PSScriptRoot 'docked_workbench.py'
$dockPidFile = Join-Path $PSScriptRoot 'state\docked_workbench.pid'
function Stop-LocalProcessTree {
    param([int]$RootPid)
    $children = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $RootPid } |
        Select-Object -ExpandProperty ProcessId
    foreach ($childPid in $children) {
        Stop-LocalProcessTree -RootPid $childPid
    }
    Stop-Process -Id $RootPid -ErrorAction SilentlyContinue
}
& $python -3.10 $dockScript --stop | Write-Output
if (Test-Path -LiteralPath $dockPidFile) {
    $dockPid = [int](Get-Content -Raw -LiteralPath $dockPidFile)
    Stop-LocalProcessTree -RootPid $dockPid
    Remove-Item -LiteralPath $dockPidFile -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'NOT_RUNNING'
    exit 0
}
$bridgePid = [int](Get-Content -Raw -LiteralPath $pidFile)
$process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
if ($process) {
    Stop-LocalProcessTree -RootPid $bridgePid
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Output "STOPPED:$bridgePid"
