# 千牛助手 残留进程清理（只杀助手自己，绝不动千牛）
#
# 用途：1.6.78 及更早版本在「首次启动」时会把自己的 QianniuAgent.exe
# （托盘/桥接/浮窗）误判成千牛仍在运行，于是永远弹「检测到旧客服助手或千牛
# 仍在运行…请先完全退出千牛」，用户去关千牛却永远关不掉（拦截它的其实是
# 看不见的托盘进程）。
#
# 本脚本：
#   * 只结束 QianniuAgent.exe（本产品），绝不结束 AliWorkbench.exe / AliRender.exe
#   * 不删除任何配置、不删 state、不动千牛数据
#   * 默认试运行；加 -Force 才真的结束
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File 救急-清理助手残留.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File 救急-清理助手残留.ps1 -Force

param([switch]$Force)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$OWN = @("qianniuagent.exe")

Write-Host "主机: $env:COMPUTERNAME"
Write-Host ("模式: " + $(if ($Force) { "真的结束（-Force）" } else { "试运行（只列出，不结束）" }))
Write-Host ""

$mine = Get-CimInstance Win32_Process -Filter "name='QianniuAgent.exe'" -ErrorAction SilentlyContinue
if (-not $mine) {
    Write-Host "没有发现助手残留进程（QianniuAgent.exe）。"
} else {
    Write-Host ("发现 " + @($mine).Count + " 个助手进程：")
    foreach ($p in $mine) {
        $cmd = [string]$p.CommandLine
        $role = "launcher(托盘)"
        if ($cmd -match '--role\s+bridge') { $role = "bridge(桥接)" }
        elseif ($cmd -match '--role\s+dock') { $role = "dock(浮窗)" }
        Write-Host ("  pid " + $p.ProcessId + "  " + $role)
    }
}

# 千牛本体：只报告，不处理
$qn = Get-CimInstance Win32_Process -Filter "name='AliWorkbench.exe' or name='AliRender.exe'" -ErrorAction SilentlyContinue
if ($qn) {
    Write-Host ""
    Write-Host ("千牛客户端在运行：" + @($qn).Count + " 个进程（本脚本不会动它）")
    Write-Host "  要启动助手前需要先完全退出它：右键托盘千牛图标 → 退出。"
}

if ($Force -and $mine) {
    Write-Host ""
    foreach ($p in $mine) {
        if ($p.ProcessId -eq $PID) { continue }
        try {
            $proc = Get-Process -Id $p.ProcessId -ErrorAction Stop
            $proc.Kill()
            $proc.WaitForExit(3000) | Out-Null
            Write-Host ("已结束 pid " + $p.ProcessId)
        } catch {
            Write-Host ("结束 pid " + $p.ProcessId + " 失败：" + $_.Exception.Message + "（试试管理员身份）")
        }
    }
    Write-Host ""
    Write-Host "现在可以双击“启动千牛客服助手.cmd”重新启动。"
}

if (-not $Force) {
    Write-Host ""
    Write-Host "确认无误后重新运行并加 -Force，或双击“救急-清理助手残留.cmd”。"
}

Read-Host "按回车退出"
