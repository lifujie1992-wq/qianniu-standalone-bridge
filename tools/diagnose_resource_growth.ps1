<#
Resource growth probe for the standalone Qianniu bridge.

Purpose: reproduce the customer report "runs fine for a while, then the whole
machine freezes" on the affected machine and capture hard numbers instead of
guesses. Runs as a normal user, needs no admin rights, no source tree.

Usage (on the customer machine):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose_resource_growth.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose_resource_growth.ps1 -Minutes 480 -IntervalSeconds 30

Outputs (on the Desktop by default):
    qn_summary.csv  - one row per sample: system memory + per-group totals
    qn_detail.csv   - one row per process per sample: pid/ws/handles/threads/cpu

Send both CSVs back. The column that matters is whether bridge/tray/dock
working set, handle count or CPU-seconds keep climbing across samples, and
whether AliRender (Qianniu render processes) count grows over time.
#>

param(
    [double]$Minutes = 480,
    [int]$IntervalSeconds = 30,
    [string]$OutDir = "$env:USERPROFILE\Desktop"
)

$ErrorActionPreference = 'Continue'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$summaryPath = Join-Path $OutDir "qn_summary_$stamp.csv"
$detailPath = Join-Path $OutDir "qn_detail_$stamp.csv"

'ts,total_proc,free_mb,python_count,python_ws_mb,python_handles,python_threads,python_cpu_s,aliworkbench_count,aliworkbench_ws_mb,alirender_count,alirender_ws_mb,msedge_count,msedge_ws_mb' |
    Set-Content -LiteralPath $summaryPath -Encoding ASCII
'ts,group,pid,start_time,ws_mb,private_mb,handles,threads,cpu_s' |
    Set-Content -LiteralPath $detailPath -Encoding ASCII

function Get-Group {
    param([string]$Pattern)
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $Pattern })
}

function Get-Totals {
    param($Procs)
    $ws = 0.0; $pv = 0.0; $hd = 0; $th = 0; $cpu = 0.0
    foreach ($p in $Procs) {
        $ws += $p.WorkingSet64
        $pv += $p.PrivateMemorySize64
        $hd += $p.HandleCount
        $th += @($p.Threads).Count
        $cpu += $p.TotalProcessorTime.TotalSeconds
    }
    return [pscustomobject]@{
        Count = @($Procs).Count
        WsMb = [math]::Round($ws / 1MB, 1)
        PrivateMb = [math]::Round($pv / 1MB, 1)
        Handles = $hd
        Threads = $th
        Cpu = [math]::Round($cpu, 1)
    }
}

function Get-FreeMb {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        return [math]::Round($os.FreePhysicalMemory / 1KB, 0)
    } catch {
        return -1
    }
}

$deadline = (Get-Date).AddMinutes($Minutes)
$sample = 0
Write-Host "probing until $deadline"
Write-Host "summary: $summaryPath"
Write-Host "detail : $detailPath"

while ((Get-Date) -lt $deadline) {
    $sample += 1
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')

    $all = @(Get-Process -ErrorAction SilentlyContinue)
    $groups = [ordered]@{
        python        = @($all | Where-Object { $_.ProcessName -match '^python$' })
        aliworkbench  = @($all | Where-Object { $_.ProcessName -match '^AliWorkbench$' })
        alirender     = @($all | Where-Object { $_.ProcessName -match '^AliRender$' })
        msedge        = @($all | Where-Object { $_.ProcessName -match '^msedge$' })
    }

    $t = @{}
    foreach ($name in $groups.Keys) {
        $stat = Get-Totals $groups[$name]
        $t[$name] = $stat
        foreach ($p in $groups[$name]) {
            $row = '{0},{1},{2},{3},{4},{5},{6},{7},{8}' -f $ts, $name, $p.Id,
                $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss'),
                [math]::Round($p.WorkingSet64 / 1MB, 1),
                [math]::Round($p.PrivateMemorySize64 / 1MB, 1),
                $p.HandleCount, @($p.Threads).Count,
                [math]::Round($p.TotalProcessorTime.TotalSeconds, 1)
            Add-Content -LiteralPath $detailPath -Value $row -Encoding ASCII
        }
    }

    $line = '{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11},{12},{13}' -f `
        $ts, $all.Count, (Get-FreeMb), `
        $t['python'].Count, $t['python'].WsMb, $t['python'].Handles, $t['python'].Threads, $t['python'].Cpu, `
        $t['aliworkbench'].Count, $t['aliworkbench'].WsMb, `
        $t['alirender'].Count, $t['alirender'].WsMb, `
        $t['msedge'].Count, $t['msedge'].WsMb
    Add-Content -LiteralPath $summaryPath -Value $line -Encoding ASCII

    Write-Host ("[{0}] sample {1}  python={2} proc/{3} MB  render={4} proc/{5} MB  edge={6} proc/{7} MB  free={8} MB" -f `
        (Get-Date).ToString('HH:mm:ss'), $sample, `
        $t['python'].Count, $t['python'].WsMb, `
        $t['alirender'].Count, $t['alirender'].WsMb, `
        $t['msedge'].Count, $t['msedge'].WsMb, (Get-FreeMb))

    Start-Sleep -Seconds $IntervalSeconds
}

Write-Host 'probe finished'
