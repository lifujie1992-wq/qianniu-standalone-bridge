# 千牛取订单自检（一键，只读，不改任何设置）
# 用法：放在安装目录所在机器上双击同名 .cmd
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Find-InstallDir {
    $candidates = @()
    if ($env:QIANNIU_AI_HOME) { $candidates += $env:QIANNIU_AI_HOME }
    $candidates += (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
    $candidates += @("C:\ai\QianniuAIService", "D:\QianniuAIService", "C:\QianniuAIService",
                     "$env:LOCALAPPDATA\QianniuAIService", "D:\ai\QianniuAIService")
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "QianniuAIService") }
    foreach ($dir in $candidates) {
        if (-not $dir) { continue }
        if (Test-Path (Join-Path $dir "config.json")) { return $dir }
    }
    # 兜底：全盘找 QianniuAgent.exe（最多 3 层）
    foreach ($root in @("C:\ai", "D:\", "C:\")) {
        if (-not (Test-Path $root)) { continue }
        $hit = Get-ChildItem -Path $root -Filter "QianniuAgent.exe" -Recurse -Depth 3 -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.DirectoryName }
    }
    return $null
}

$dir = Find-InstallDir
$out = [ordered]@{ host = $env:COMPUTERNAME; time = (Get-Date).ToString("s"); install_dir = $dir }
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("主机: $($env:COMPUTERNAME)   时间: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))")
$lines.Add("安装目录: $dir")

if (-not $dir) {
    $lines.Add("")
    $lines.Add("!! 没找到安装目录（config.json 不存在）。请确认助手已安装。")
    $lines | ForEach-Object { Write-Host $_ }
} else {
    $cfg = Get-Content (Join-Path $dir "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $token = $cfg.workbench_token
    $port = if ($cfg.workbench_port) { [int]$cfg.workbench_port } else { 18776 }
    $base = "http://127.0.0.1:$port"
    $headers = @{ "X-Workbench-Token" = $token }

    function Get-Json([string]$path, [int]$timeout = 30) {
        try {
            return Invoke-RestMethod -Uri ($base + $path) -Headers $headers -TimeoutSec $timeout -Method Get
        } catch {
            $msg = $_.Exception.Message
            try {
                $resp = $_.Exception.Response
                if ($resp) {
                    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
                    $body = $reader.ReadToEnd()
                    if ($body) { $msg = "$msg | 服务器: $($body.Substring(0, [Math]::Min(300, $body.Length)))" }
                }
            } catch { }
            return [pscustomobject]@{ ok = $false; error = $msg }
        }
    }

    # 1) 版本与挂钩
    $status = Get-Json "/api/v1/status"
    $bridge = $status.bridge
    $out["agent"] = [ordered]@{
        version = $status.version; build_hash = $status.build_hash
        ok = $status.ok; error = $status.error
    }
    $lines.Add("助手版本: $($status.version)  构建: $($status.build_hash)")
    if ($bridge) {
        $hs = $bridge.browser.hook_summary
        $conn = $bridge.browser.connected
        $lines.Add("页面连接: $conn   挂钩: $($hs.hooked)/$($hs.pages)   判定: $($hs.chat_page_present)")
        $out["hook"] = [ordered]@{ connected = $conn; summary = $hs }
        if ($bridge.accounts) {
            $known = @($bridge.accounts.known)
            if ($known.Count -gt 0) { $lines.Add("账号: $($known -join ', ')") } else { $lines.Add("账号: （还没识别到）") }
        }
    } else {
        $lines.Add("!! 状态接口没返回 bridge 段：$($status.error)")
    }

    # 2) 通道
    $ch = Get-Json "/api/v1/catalog/channels"
    if ($ch.ok) {
        $lines.Add("")
        $lines.Add("页面能力: imsdk=$($ch.imsdk_typeof)/$($ch.imsdk_invoke_typeof)  imsdk2=$($ch.imsdk2_typeof)/$($ch.imsdk2_invoke_typeof)  QN=$($ch.QN_typeof)/app.invoke=$($ch.QN_app_invoke_typeof)  workbench=$($ch.workbench_typeof)")
        $lines.Add("          页面: $($ch.page_title)  $($ch.page_url)")
    } else {
        $lines.Add("!! 通道探测失败: $($ch.error)")
    }
    $out["channels"] = $ch

    # 3) 商品（不需要买家，先验证 mtop 本身通不通）
    $goods = Get-Json "/api/v1/catalog/goods?page_no=1&page_size=3" 45
    if ($goods.ok) {
        $lines.Add("商品查询: 成功（本页 $($goods.goods.Count) 条 / 共 $($goods.total) 条）")
    } else {
        $lines.Add("商品查询: 失败 -> $($goods.error)")
    }
    $out["goods"] = $goods

    # 4) 订单：从最近会话里取一个买家
    $sessions = Get-Json "/api/v1/sessions"
    $buyer = ""
    if ($sessions.ok -and $sessions.sessions) {
        foreach ($s in $sessions.sessions) {
            if ($s.buyer_id -match '^(\d{6,22})\.1-') { $buyer = $matches[1]; break }
        }
    }
    $out["buyer_encrypt_id"] = $buyer
    if ($buyer) {
        $orders = Get-Json "/api/v1/catalog/orders?buyer_encrypt_id=$buyer" 45
        if ($orders.ok) {
            $src = if ($orders.source) { " 来源=$($orders.source)" } else { "" }
            $lines.Add("订单查询: 成功（买家 $buyer，$($orders.count) 单$src）")
        } else {
            $lines.Add("订单查询: 失败（买家 $buyer）-> $($orders.error)")
        }
        $out["orders"] = $orders
    } else {
        $lines.Add("订单查询: 跳过（还没有任何会话记录；让客服随便打开一个买家会话再跑一次）")
        $out["orders"] = [ordered]@{ ok = $null; skipped = "no_session" }
    }

    # 5) 服务端统计（看最近真正取单用的是什么通道）
    $catalog = Get-Json "/api/v1/catalog"
    if ($catalog.ok) {
        $m = $catalog.methods
        $lines.Add("")
        $lines.Add("累计请求: $($catalog.requests)   最近成功: $([DateTimeOffset]::FromUnixTimeMilliseconds([long]($catalog.last_success_at * 1000)).LocalDateTime)")
        $lines.Add("最近通道: $($m.last_channel)   最近错误: $($catalog.last_error)")
    }
    $out["catalog"] = $catalog
}

$lines.Add("")
$lines.Add("==== 结论 ====")
$ok = ($out["goods"].ok -eq $true) -or ($out["orders"].ok -eq $true)
if ($ok) {
    $lines.Add("能取数据：这条机器正常。把本报告发回即可。")
} else {
    $lines.Add("取不到数据：把本报告（.txt 和 .json 两个文件）发给技术同学。")
}
$lines | ForEach-Object { Write-Host $_ }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$desk = [Environment]::GetFolderPath("Desktop")
$txt = Join-Path $desk "千牛取订单自检-$($env:COMPUTERNAME)-$stamp.txt"
$json = Join-Path $desk "千牛取订单自检-$($env:COMPUTERNAME)-$stamp.json"
$lines | Set-Content -Path $txt -Encoding UTF8
try { ($out | ConvertTo-Json -Depth 8) | Set-Content -Path $json -Encoding UTF8 } catch { }
Write-Host ""
Write-Host "报告已生成："
Write-Host "  $txt"
Write-Host "  $json"
