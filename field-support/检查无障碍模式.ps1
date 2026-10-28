# 千牛「无障碍模式」检查（只读，不依赖助手是否在运行）
# 直接读 AliWorkbenchData\NewAppData\globalConfig\Global.db 里的
# key2value.EnableAccessibility：true = 开，false = 关。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$roots = @()
if ($env:QNQIANNIU_DATA_DIR) { $roots += $env:QNQIANNIU_DATA_DIR }
foreach ($drive in @("D:", "C:", "E:", "F:")) {
    if (-not (Test-Path ($drive + ""))) { continue }
    $roots += ($drive + "\AliWorkbenchData")
}
if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "AliWorkbenchData") }

$db = $null
foreach ($root in $roots) {
    $candidate = Join-Path $root "NewAppData\globalConfig\Global.db"
    if (Test-Path $candidate) { $db = $candidate; break }
}

Write-Host "主机: $env:COMPUTERNAME"
if (-not $db) {
    Write-Host "!! 没找到 Global.db（千牛可能没登录过，或数据目录不在默认位置）"
    Read-Host "按回车退出"
    exit 1
}
Write-Host "配置: $db"

# 用 SQLite 记录里的原始字节判断：键名后紧跟的值就是 true/false
# 千牛运行时独占 db，先复制一份再读（只读操作，不改原文件）
$tmp = Join-Path $env:TEMP ("qn-acc-" + [Guid]::NewGuid().ToString("N") + ".db")
$copy = $db
try {
    $fs = [System.IO.File]::Open($db, "Open", "Read", "ReadWrite")
    $out = [System.IO.File]::Create($tmp)
    $fs.CopyTo($out)
    $out.Close(); $fs.Close()
    $copy = $tmp
} catch {
    # 完全共享不了就退化为普通复制
    try { Copy-Item $db $tmp -Force; $copy = $tmp } catch { $copy = $db }
}
$bytes = [System.IO.File]::ReadAllBytes($copy)
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
$text = [System.Text.Encoding]::ASCII.GetString($bytes)
$key = "EnableAccessibility"
$index = $text.LastIndexOf($key)

if ($index -lt 0) {
    Write-Host ""
    Write-Host "无障碍模式: 没有这个设置项（等于未开启）"
    Write-Host "请到 千牛 → 设置 → 通用 → 无障碍模式 打开，然后重启千牛。"
} else {
    $tail = $text.Substring($index, [Math]::Min(120, $text.Length - $index))
    $value = "读不到"
    if ($tail -match 'true') { $value = "开" }
    elseif ($tail -match 'false') { $value = "关" }
    Write-Host ""
    Write-Host "无障碍模式: $value"
    if ($value -eq "开") {
        Write-Host "→ 这个开关是对的。若仍取不到订单，请跑 取不到订单时点这里-自检.cmd 把报告发回。"
    } else {
        Write-Host ""
        Write-Host "→ 这就是取不到订单的原因："
        Write-Host "   千牛 → 设置 → 通用 → 无障碍模式 → 打开 → 重启千牛"
        Write-Host "   不开这个，千牛不会把 workbench 注入页面，任何版本都取不到订单。"
    }
}
Write-Host ""
Read-Host "按回车退出"
