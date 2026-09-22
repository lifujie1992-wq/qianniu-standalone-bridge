param(
    [switch]$Execute,

    # 旧环境（探域客户端 / 上一代桥）路径。请按实际部署传入；默认为空表示不参与回滚。
    [string]$LegacyTanyuRoot = '',
    [string]$LegacyAgentRoot = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'runtime'
$tanyuExecutable = if ($LegacyTanyuRoot) {
    Get-ChildItem -LiteralPath $LegacyTanyuRoot -Recurse -File -Filter 'TyAgent.exe' |
        Where-Object { $_.Directory.Name -eq 'tanyu2.9.1' } |
        Select-Object -First 1
}
$tanyuExe = if ($tanyuExecutable) { $tanyuExecutable.FullName } else { '' }
$oldBridgeExe = if ($LegacyAgentRoot) { Join-Path $LegacyAgentRoot 'QianniuBridgeAgent.exe' } else { '' }

if (-not $Execute) {
    Write-Output 'PREVIEW_ONLY: stops the standalone runtime, disables delivery, and restarts the previous Tanyu/bridge processes.'
    exit 0
}

$configPath = Join-Path $projectRoot 'config.json'
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
$config.delivery_enabled = $false
$config.auto_launch_qianniu = $false
$config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8

& (Join-Path $projectRoot 'stop.ps1') | Out-Null
Get-CimInstance Win32_Process | Where-Object {
    ([string]$_.ExecutablePath).StartsWith($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Unregister-ScheduledTask -TaskName 'QianniuStandaloneBridge' -Confirm:$false -ErrorAction SilentlyContinue
Enable-ScheduledTask -TaskName 'AliUpdater' -ErrorAction SilentlyContinue | Out-Null

if (Test-Path -LiteralPath $tanyuExe) {
    Start-Process -FilePath $tanyuExe -WorkingDirectory (Split-Path $tanyuExe) | Out-Null
}
if (Test-Path -LiteralPath $oldBridgeExe) {
    Start-Process -FilePath $oldBridgeExe -WorkingDirectory (Split-Path $oldBridgeExe) -WindowStyle Hidden | Out-Null
}

Write-Output 'ROLLBACK_STARTED'
