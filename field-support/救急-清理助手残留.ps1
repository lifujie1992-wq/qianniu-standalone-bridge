# 千牛助手 残留进程清理（只杀助手自己，绝不动千牛）
# 1.6.80 版：额外列出每个进程的完整路径和该目录的版本号，
# 专门用来抓「装了新版却还在跑旧目录 exe」的情况。
param([switch]$Force)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
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
        Write-Host ("      路径: " + $p.ExecutablePath)
        $info = Join-Path (Split-Path -Parent $p.ExecutablePath) "build-info.json"
        if (Test-Path $info) {
            try {
                $j = Get-Content $info -Raw -Encoding UTF8 | ConvertFrom-Json
                Write-Host ("      版本: " + $j.version + "  (commit " + $j.commit.Substring(0,7) + ")")
            } catch { Write-Host "      版本: build-info.json 读取失败" }
        } else {
            Write-Host "      版本: 没有 build-info.json（很可能是 1.6.78 之前的旧安装/解压包）"
        }
    }
}
$qn = Get-CimInstance Win32_Process -Filter "name='AliWorkbench.exe' or name='AliRender.exe'" -ErrorAction SilentlyContinue
if ($qn) {
    Write-Host ""
    Write-Host ("千牛客户端在运行：" + @($qn).Count + " 个进程（本脚本不会动它）")
}
if ($Force -and $mine) {
    Write-Host ""
    foreach ($p in $mine) {
        if ($p.ProcessId -eq $PID) { continue }
        try {
            $proc = Get-Process -Id $p.ProcessId -ErrorAction Stop
            $proc.Kill(); $proc.WaitForExit(3000) | Out-Null
            Write-Host ("已结束 pid " + $p.ProcessId)
        } catch {
            Write-Host ("结束 pid " + $p.ProcessId + " 失败：" + $_.Exception.Message + "（试试管理员身份）")
        }
    }
    Write-Host ""
    Write-Host "现在从 开始菜单 → 千牛 AI 客服助手 → 启动客服助手 启动（不要用旧文件夹里的 cmd）。"
}
if (-not $Force) {
    Write-Host ""
    Write-Host "确认无误后重新运行并加 -Force，或双击 救急-清理助手残留.cmd。"
}
Read-Host "按回车退出"
