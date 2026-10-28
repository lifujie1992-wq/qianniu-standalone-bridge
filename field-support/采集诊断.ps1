# 千牛助手 现场诊断采集（只读）
# 不安装任何东西、不重启千牛、不模拟键鼠、不截屏、不发消息。
# 产物：桌面 千牛诊断-<机器名>-<时间>.zip
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$work = Join-Path $env:TEMP "qn-diag-$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$lines = New-Object System.Collections.Generic.List[string]
function Say($text) { Write-Host $text; $lines.Add($text) | Out-Null }

# ---------- 1. 找安装目录 ----------
function Find-InstallDir {
    $c = @()
    if ($env:QIANNIU_AI_HOME) { $c += $env:QIANNIU_AI_HOME }
    $c += (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
    $c += @("C:\ai\QianniuAIService", "D:\QianniuAIService", "C:\QianniuAIService",
            "D:\ai\QianniuAIService", "$env:LOCALAPPDATA\QianniuAIService")
    if ($env:ProgramFiles) { $c += (Join-Path $env:ProgramFiles "QianniuAIService") }
    foreach ($d in $c) { if ($d -and (Test-Path (Join-Path $d "config.json"))) { return $d } }
    return $null
}
$dir = Find-InstallDir
Say "主机      : $($env:COMPUTERNAME)"
Say "时间      : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Say "安装目录  : $(if ($dir) { $dir } else { '未找到' })"

if (-not $dir) {
    Say ""
    Say "!! 没找到 config.json，请确认助手已安装，或手工告知安装目录。"
    $lines | Set-Content (Join-Path $work "summary.txt") -Encoding UTF8
    Compress-Archive -Path "$work\*" -DestinationPath (Join-Path ([Environment]::GetFolderPath("Desktop")) "千牛诊断-$($env:COMPUTERNAME)-$stamp.zip") -Force
    Read-Host "已生成报告，按回车退出"
    exit 1
}

# ---------- 2. 读配置（token 一律不落盘） ----------
$cfg = Get-Content (Join-Path $dir "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$port = if ($cfg.workbench_port) { [int]$cfg.workbench_port } else { 18776 }
$apiPort = if ($cfg.api_port) { [int]$cfg.api_port } else { 42111 }
$base = "http://127.0.0.1:$port"
$headers = @{ "X-Workbench-Token" = $cfg.workbench_token }

function Get-Api([string]$path, [int]$timeout = 40) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-RestMethod -Uri ($base + $path) -Headers $headers -TimeoutSec $timeout -Method Get
        $sw.Stop()
        return [pscustomobject]@{ ok = $true; ms = $sw.ElapsedMilliseconds; data = $r }
    } catch {
        $sw.Stop()
        $msg = $_.Exception.Message
        try {
            $resp = $_.Exception.Response
            if ($resp) {
                $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
                $body = $sr.ReadToEnd()
                if ($body) { $msg = "$msg | $($body.Substring(0, [Math]::Min(400, $body.Length)))" }
            }
        } catch { }
        return [pscustomobject]@{ ok = $false; ms = $sw.ElapsedMilliseconds; error = $msg }
    }
}

# ---------- 3. 助手状态 ----------
$status = Get-Api "/api/v1/status" 60
if ($status.ok) {
    $b = $status.data.bridge
    Say "助手版本  : $($status.data.version)  构建 $($status.data.build_hash)  状态接口 $($status.ms)ms"
    if ($b) {
        $hs = $b.browser.hook_summary
        Say "挂钩      : $($hs.hooked)/$($hs.pages)  chat_page_present=$($hs.chat_page_present)  页面连接 $($b.browser.connected)"
        $acc = @($b.accounts.known) | Where-Object { $_ }
        Say "账号      : $(if ($acc) { $acc -join ', ' } else { '（未识别）' })"
        Say "投递队列  : pending=$($b.delivery.pending)"
        Say "上下文补全: requests=$($b.context_enrich.requests) successes=$($b.context_enrich.successes) last_error=$($b.context_enrich.last_error)"
        Say "CDP       : enabled=$($b.cdp.enabled) port=$($b.cdp.port) last_error=$($b.cdp.last_error)"
        $cp = $b.client_support
        if ($cp) { Say "客户端支持: active=$($cp.active_build) supported=$($cp.active_build_supported) problems=$(@($cp.problems).Count)" }
        $pg = @($b.browser.process_pages)
        Say "页面清单  : $($pg.Count) 个"
        foreach ($p in $pg | Select-Object -First 20) {
            Say ("            - {0,-6} imsdk={1,-5} {2}" -f $p.kind, $p.has_imsdk, $p.url)
        }
    }
    ($status.data | ConvertTo-Json -Depth 10) | Set-Content (Join-Path $work "status.json") -Encoding UTF8
} else {
    Say "助手状态  : FAIL ($($status.ms)ms) -> $($status.error)"
    Say "            （status 超过 10 秒多半是 1.6.49 及更早的已知缺陷）"
}

# ---------- 4. 页面能力（1.6.42+） ----------
$ch = Get-Api "/api/v1/catalog/channels"
if ($ch.ok) {
    $d = $ch.data
    Say ""
    Say "页面能力  : imsdk=$($d.imsdk_typeof)/$($d.imsdk_invoke_typeof)  imsdk2=$($d.imsdk2_typeof)/$($d.imsdk2_invoke_typeof)"
    Say "            QN=$($d.QN_typeof)/app.invoke=$($d.QN_app_invoke_typeof)  QNAbilityCenter=$($d.ability_center_typeof)  abilitycenter=$($d.abilitycenter_typeof)  workbench=$($d.workbench_typeof)"
    Say "            page=$($d.page_title)  $($d.page_url)"
    ($d | ConvertTo-Json -Depth 6) | Set-Content (Join-Path $work "channels.json") -Encoding UTF8
} else {
    Say ""
    Say "页面能力  : 接口不可用（1.6.42 以下没有此接口）-> $($ch.error)"
}

# ---------- 5. 实调商品（不需要买家） ----------
$goods = Get-Api "/api/v1/catalog/goods?page_no=1&page_size=3" 60
if ($goods.ok -and $goods.data.ok) {
    Say "商品查询  : OK 本页 $(@($goods.data.goods).Count) 条 / 共 $($goods.data.total) 条  ($($goods.ms)ms)"
} else {
    Say "商品查询  : FAIL -> $(if ($goods.ok) { $goods.data.error } else { $goods.error })"
}
if ($goods.ok) { ($goods.data | ConvertTo-Json -Depth 8) | Set-Content (Join-Path $work "goods.json") -Encoding UTF8 }

# ---------- 6. 实调订单（从最近会话取买家） ----------
$sessions = Get-Api "/api/v1/sessions"
$buyer = ""
if ($sessions.ok -and $sessions.data.sessions) {
    foreach ($s in $sessions.data.sessions) {
        if ($s.buyer_id -match '^(\d{6,22})\.1-') { $buyer = $matches[1]; break }
    }
    ($sessions.data | ConvertTo-Json -Depth 6) | Set-Content (Join-Path $work "sessions.json") -Encoding UTF8
}
if ($buyer) {
    $orders = Get-Api "/api/v1/catalog/orders?buyer_encrypt_id=$buyer" 60
    if ($orders.ok -and $orders.data.ok) {
        Say "订单查询  : OK 买家 $buyer 共 $($orders.data.count) 单  ($($orders.ms)ms)"
    } else {
        Say "订单查询  : FAIL 买家 $buyer -> $(if ($orders.ok) { $orders.data.error } else { $orders.error })"
    }
    if ($orders.ok) { ($orders.data | ConvertTo-Json -Depth 8) | Set-Content (Join-Path $work "orders.json") -Encoding UTF8 }

    # 逐通道验证（1.6.47+）：只在"订单取不到"时才做，成功就不用浪费时间
    $ordersOk = ($orders.ok -and $orders.data.ok)
    if (-not $ordersOk) {
        foreach ($c in @("imsdk", "imsdk2", "qn_app", "QNAbilityCenter", "abilitycenter")) {
            $r = Get-Api "/api/v1/catalog/orders?buyer_encrypt_id=$buyer&channel=$c" 12
            if ($r.ok -and $r.data.ok) { Say ("            channel {0,-15} -> OK {1} 单" -f $c, $r.data.count) }
            else {
                $msg = if ($r.ok) { [string]$r.data.error } else { [string]$r.error }
                if (-not $msg) { $msg = "无响应" }
                Say ("            channel {0,-15} -> {1}" -f $c, $msg.Substring(0, [Math]::Min(150, $msg.Length)))
            }
        }
    }
} else {
    Say "订单查询  : 跳过（没有会话记录，让客服打开一个买家会话后再跑一次）"
}

# ---------- 7. 目录累计统计 ----------
$cat = Get-Api "/api/v1/catalog"
if ($cat.ok) {
    Say ""
    Say "累计请求  : $($cat.data.requests)  最近通道=$($cat.data.methods.last_channel)  最近错误=$($cat.data.last_error)"
    ($cat.data | ConvertTo-Json -Depth 6) | Set-Content (Join-Path $work "catalog.json") -Encoding UTF8
}

# ---------- 8. 客户端指纹 ----------
Say ""
Say "---- 客户端指纹 ----"
$runtime = Join-Path $dir "runtime"
$ini = Join-Path $runtime "AliWorkbench.ini"
if (Test-Path $ini) {
    $ver = (Select-String -Path $ini -Pattern '^\s*Version=(.+)$' | Select-Object -First 1).Matches.Groups[1].Value
    Say "千牛版本  : $ver"
}
$verDirs = @()
if (Test-Path $runtime) {
    $verDirs = Get-ChildItem $runtime -Directory -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match '^\d+\.\d+' } | Select-Object -ExpandProperty Name
    Say "runtime   : $($verDirs -join ', ')"
}
foreach ($v in $verDirs) {
    $dll = Join-Path $runtime "$v\AppBiz.dll"
    $zip = Join-Path $runtime "$v\Resources\newWebui\webui.zip"
    if (Test-Path $dll) {
        $h = (Get-FileHash $dll -Algorithm SHA256).Hash.Substring(0, 16)
        Say ("            {0,-10} AppBiz.dll {1,10} B  sha256:{2}" -f $v, (Get-Item $dll).Length, $h)
    }
    if (Test-Path $zip) {
        $h2 = (Get-FileHash $zip -Algorithm SHA256).Hash.Substring(0, 16)
        $kind = "?"
        try {
            $z = [System.IO.Compression.ZipFile]::OpenRead($zip)
            $names = $z.Entries | ForEach-Object { $_.FullName }
            $hasRecent = @($names | Where-Object { $_ -like "*web_chat-packer/recent.html" }).Count -gt 0
            $hasDxh5   = @($names | Where-Object { $_ -like "*dx-h5/index.html" }).Count -gt 0
            $kind = if ($hasRecent -and $hasDxh5) { "recent+dx-h5" } elseif ($hasRecent) { "recent" } elseif ($hasDxh5) { "dx-h5 only" } else { "unknown" }
            $z.Dispose()
        } catch { $kind = "unreadable: $($_.Exception.Message)" }
        Say ("            {0,-10} webui.zip {1,10} B  sha256:{2}  页面类型={3}" -f $v, (Get-Item $zip).Length, $h2, $kind)
    }
}

# ---------- 9. 日志尾部 ----------
$log = Join-Path $dir "state\standalone_bridge.log"
if (Test-Path $log) {
    Get-Content $log -Tail 300 -Encoding UTF8 | Set-Content (Join-Path $work "bridge-log-tail.txt") -Encoding UTF8
    Say ""
    Say "日志尾部  : 已采集（300 行）"
}
Get-ChildItem (Join-Path $dir "state") -Filter "*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne "standalone_bridge.log" } |
    ForEach-Object { [pscustomobject]@{ file = $_.Name; MB = [math]::Round($_.Length/1MB, 2); modified = $_.LastWriteTime } } |
    ConvertTo-Json -Depth 3 | Set-Content (Join-Path $work "logs-index.json") -Encoding UTF8

# 配置脱敏备份（token 一律替换）
$safe = $cfg.PSObject.Copy()
foreach ($k in @("workbench_token","api_token","browser_token","gateway_agent_token","brain_agent_token")) {
    if ($safe.PSObject.Properties.Name -contains $k) { $safe.$k = "***" }
}
($safe | ConvertTo-Json -Depth 5) | Set-Content (Join-Path $work "config-redacted.json") -Encoding UTF8

# ---------- 10. 结论 + 打包 ----------
Say ""
Say "==== 结论 ===="
$good = ($goods.ok -and $goods.data.ok) -or ($orders.ok -and $orders.data.ok)
if ($good) { Say "能取到数据。" } else { Say "取不到数据，请把本报告发回技术同学。" }
$lines | Set-Content (Join-Path $work "summary.txt") -Encoding UTF8

$zipOut = Join-Path ([Environment]::GetFolderPath("Desktop")) "千牛诊断-$($env:COMPUTERNAME)-$stamp.zip"
if (Test-Path $zipOut) { Remove-Item $zipOut -Force }
Compress-Archive -Path "$work\*" -DestinationPath $zipOut -Force
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "报告已生成："
Write-Host "  $zipOut"
