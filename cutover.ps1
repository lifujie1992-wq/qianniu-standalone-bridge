param(
    [switch]$Execute,

    # 旧环境（探域客户端 / 上一代桥）路径与本机大脑地址。
    # 请按实际部署传入；默认值为占位示例，不指向任何真实主机。
    [string]$LegacyTanyuRoot = '',
    [string]$LegacyQianniuRoot = '',
    [string]$LegacyAgentRoot = '',
    [string]$BrainBackend = 'http://YOUR_BRAIN_HOST:18765'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$stateRoot = Join-Path $projectRoot 'state'
$configPath = Join-Path $projectRoot 'config.json'
$runtimeRoot = (Join-Path $projectRoot 'runtime')
$tanyuExecutable = if ($LegacyTanyuRoot) {
    Get-ChildItem -LiteralPath $LegacyTanyuRoot -Recurse -File -Filter 'TyAgent.exe' |
        Where-Object { $_.Directory.Name -eq 'tanyu2.9.1' } |
        Select-Object -First 1
}
if (-not $tanyuExecutable) {
    throw 'Cannot locate the legacy Tanyu process root. Pass -LegacyTanyuRoot <path> pointing at the legacy agent install directory.'
}
$tanyuRoot = $tanyuExecutable.Directory.FullName
if (-not $LegacyQianniuRoot -or -not $LegacyAgentRoot) {
    throw 'Pass -LegacyQianniuRoot <path> and -LegacyAgentRoot <path> for the previous-generation bridge deployment.'
}
$oldQianniuRoot = $LegacyQianniuRoot
$oldBridgeExe = Join-Path $LegacyAgentRoot 'QianniuBridgeAgent.exe'
$gatewayExe = Join-Path $LegacyAgentRoot 'LocalSeatGateway.exe'
$gatewayConfig = Join-Path $LegacyAgentRoot 'bridge_config.taobao.json'

function Get-ScopedProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $path = [string]$_.ExecutablePath
        $path.StartsWith($tanyuRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $path.StartsWith($oldQianniuRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $path.Equals($oldBridgeExe, [StringComparison]::OrdinalIgnoreCase)
    }
}

function Get-StandaloneRuntimeProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        ([string]$_.ExecutablePath).StartsWith($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)
    }
}

function Set-BridgeMode([bool]$deliveryEnabled, [bool]$autoLaunch) {
    $config = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
    $config.delivery_enabled = $deliveryEnabled
    $config.auto_launch_qianniu = $autoLaunch
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
}

function Test-Gateway {
    try {
        $connection = Get-NetTCPConnection -State Listen -LocalPort 18766 -ErrorAction Stop |
            Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::') } |
            Select-Object -First 1
        return $null -ne $connection
    } catch {
        return $false
    }
}

function Start-GatewayIfNeeded {
    if (Test-Gateway) {
        return
    }
    if (-not (Test-Path -LiteralPath $gatewayExe) -or -not (Test-Path -LiteralPath $gatewayConfig)) {
        throw 'Local seat gateway is unavailable and its executable/config cannot be found.'
    }
    $arguments = @(
        '--host', '127.0.0.1',
        '--port', '18766',
        '--backend', $BrainBackend,
        '--ui-role', 'seat',
        '--bridge-config', $gatewayConfig
    )
    Start-Process -FilePath $gatewayExe -ArgumentList $arguments -WorkingDirectory (Split-Path $gatewayExe) -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not (Test-Gateway)) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Gateway)) {
        throw 'Local seat gateway did not bind to 127.0.0.1:18766.'
    }
}

$targets = @(Get-ScopedProcesses | Select-Object ProcessId, ParentProcessId, Name, ExecutablePath)
$preview = [ordered]@{
    mode = if ($Execute) { 'execute' } else { 'preview' }
    stop_processes = $targets
    keep_gateway = $true
    standalone_runtime = Join-Path $runtimeRoot 'AliWorkbench.exe'
    delivery_after_cutover = $true
    send_after_cutover = $false
}
if (-not $Execute) {
    $preview | ConvertTo-Json -Depth 5
    Write-Output 'PREVIEW_ONLY: rerun with -Execute during a maintenance window.'
    exit 0
}

if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot 'AliWorkbench.exe'))) {
    throw 'Standalone Qianniu runtime is incomplete.'
}
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null

& py.exe -3.10 -m unittest discover -s (Join-Path $projectRoot 'tests') -v
if ($LASTEXITCODE -ne 0) {
    throw 'Standalone preflight tests failed.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$snapshotPath = Join-Path $stateRoot "cutover-$stamp.json"
$configBackup = Join-Path $stateRoot "config-before-cutover-$stamp.json"
Copy-Item -LiteralPath $configPath -Destination $configBackup
$preview.config_backup = $configBackup
$preview | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $snapshotPath -Encoding UTF8

& (Join-Path $projectRoot 'stop.ps1') | Out-Null

$targets = @(Get-ScopedProcesses)
if ($targets.Count -gt 0) {
    $targets | Sort-Object ProcessId -Descending | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
$standaloneTargets = @(Get-StandaloneRuntimeProcesses)
if ($standaloneTargets.Count -gt 0) {
    $standaloneTargets | Sort-Object ProcessId -Descending | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 2

$survivors = @(Get-ScopedProcesses)
if ($survivors.Count -gt 0) {
    throw "Scoped legacy processes survived: $($survivors.ProcessId -join ',')"
}

$updater = Get-ScheduledTask -TaskName 'AliUpdater' -ErrorAction SilentlyContinue
if ($updater) {
    $usesOldRuntime = @($updater.Actions | Where-Object {
        ([string]$_.Execute).StartsWith($oldQianniuRoot, [StringComparison]::OrdinalIgnoreCase)
    }).Count -gt 0
    if ($usesOldRuntime) {
        Disable-ScheduledTask -TaskName 'AliUpdater' | Out-Null
    }
}

Start-GatewayIfNeeded
Set-BridgeMode -deliveryEnabled $true -autoLaunch $true

$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $projectRoot 'start.ps1') + '"'
)
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName 'QianniuStandaloneBridge' -Action $taskAction -Trigger $taskTrigger -Description 'Standalone Qianniu message bridge' -Force | Out-Null

& (Join-Path $projectRoot 'start.ps1') | Out-Null
$deadline = (Get-Date).AddSeconds(180)
$status = $null
while ((Get-Date) -lt $deadline) {
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:42111/api/v1/status' -TimeoutSec 2
        $runtimeProcess = if ($status.cdp.host_pid) {
            Get-CimInstance Win32_Process -Filter "ProcessId=$($status.cdp.host_pid)" -ErrorAction SilentlyContinue
        }
        $cleanOwner = $runtimeProcess -and ([string]$runtimeProcess.ExecutablePath).StartsWith(
            $runtimeRoot, [StringComparison]::OrdinalIgnoreCase
        )
        $nativeHealthy = (-not $status.native.enabled) -or $status.native.ready
        if ($status.ok -and $status.browser.connected -ge 1 -and $nativeHealthy -and $cleanOwner) {
            break
        }
    } catch {
        $status = $null
    }
    Start-Sleep -Milliseconds 500
}

$nativeHealthy = $status -and ((-not $status.native.enabled) -or $status.native.ready)
if (-not $status -or -not $status.ok -or $status.browser.connected -lt 1 -or -not $nativeHealthy -or -not $cleanOwner) {
    Set-BridgeMode -deliveryEnabled $false -autoLaunch $false
    & (Join-Path $projectRoot 'stop.ps1') | Out-Null
    @(Get-StandaloneRuntimeProcesses) | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    & (Join-Path $projectRoot 'start.ps1') | Out-Null
    throw "Cutover health check failed. Delivery was disabled. Snapshot: $snapshotPath"
}

[ordered]@{
    ok = $true
    snapshot = $snapshotPath
    runtime_host_pid = $status.cdp.host_pid
    injection_mode = if ($status.cdp.enabled) { 'cdp' } else { 'packaged-webui' }
    browser_connected = $status.browser.connected
    native_enabled = $status.native.enabled
    native_ready = $status.native.ready
    delivery_enabled = $status.delivery.enabled
    send_enabled = $status.send_enabled
} | ConvertTo-Json -Depth 4
