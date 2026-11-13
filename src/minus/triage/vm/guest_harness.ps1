param([int]$TimeoutSeconds = 180)

# Runs inside the guest. Collects evidence, then writes exactly one report.
# Every failure is caught and still produces a report with completed=false: a
# silent exit would look like a detonation that observed nothing, which is the
# one outcome that must never be manufactured.

$ErrorActionPreference = 'Continue'
$root = 'C:\triage'
$reportPath = Join-Path $root 'report.json'
$inputDir = Join-Path $root 'input'
$workDir = Join-Path $root 'work'

$defender = @()
$processes = @()
$network = @()
$fileWrites = @()
$registry = @()
$notes = @()
$launched = 0
$completed = $false
$start = Get-Date

function Save-Report {
    param([bool]$Ok)
    $payload = [ordered]@{
        completed          = $Ok
        artifacts_launched = $launched
        defender           = @($defender)
        processes          = @($processes)
        network_endpoints  = @($network)
        file_writes        = @($fileWrites)
        registry_writes    = @($registry)
        notes              = ($notes -join '; ')
        started_at         = $start.ToString('o')
        finished_at        = (Get-Date).ToString('o')
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -Path $reportPath -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $workDir | Out-Null

    $baselineProcesses = @(Get-Process | Select-Object -ExpandProperty Id)
    $runKeys = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
    )
    $baselineRun = @{}
    foreach ($key in $runKeys) {
        if (Test-Path $key) {
            $props = Get-ItemProperty -Path $key
            foreach ($p in $props.PSObject.Properties) {
                if ($p.Name -notlike 'PS*') { $baselineRun[$p.Name] = $p.Value }
            }
        }
    }

    $sysmon = Get-Service -Name Sysmon64 -ErrorAction SilentlyContinue
    $hasSysmon = ($null -ne $sysmon) -and ($sysmon.Status -eq 'Running')
    if (-not $hasSysmon) { $notes += 'no Sysmon: process/network telemetry is polling-based' }

    # Defender scans without remediation so the sample and its verdict survive.
    $mp = Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe'
    if (Test-Path $mp) {
        & $mp -Scan -ScanType 3 -File $inputDir -DisableRemediation | Out-Null
        $defender = @(Get-MpThreatDetection -ErrorAction SilentlyContinue |
            ForEach-Object { $_.ThreatName } | Select-Object -Unique)
    } else {
        $notes += 'MpCmdRun.exe not found'
    }

    $launchable = @('exe', 'com', 'bat', 'cmd', 'ps1', 'scr', 'lnk', 'msi')
    foreach ($file in Get-ChildItem -Path $inputDir -Recurse -File -ErrorAction SilentlyContinue) {
        $ext = $file.Extension.TrimStart('.').ToLowerInvariant()
        if ($launchable -notcontains $ext) { continue }

        $stem = [IO.Path]::GetFileNameWithoutExtension($file.Name)
        $dir = Join-Path $workDir $stem
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $launched++

        $p = Start-Process -FilePath $file.FullName -WorkingDirectory $dir -PassThru -ErrorAction SilentlyContinue
        if ($null -ne $p -and -not $p.WaitForExit($TimeoutSeconds * 1000)) {
            taskkill /T /F /PID $p.Id | Out-Null
            $notes += "killed $($file.Name) after ${TimeoutSeconds}s"
        }
    }

    if ($hasSysmon) {
        Start-Sleep -Seconds 2
        $events = @(Get-WinEvent -FilterHashtable @{
            LogName   = 'Microsoft-Windows-Sysmon/Operational'
            Id        = 1, 3, 11, 13
            StartTime = $start
        } -ErrorAction SilentlyContinue)
        foreach ($event in $events) {
            $xml = [xml]$event.ToXml()
            $data = @{}
            foreach ($node in $xml.Event.EventData.Data) { $data[$node.Name] = $node.'#text' }
            switch ($event.Id) {
                1 { if ($data['Image']) { $processes += $data['Image'] } }
                3 { if ($data['DestinationIp']) { $network += "$($data['DestinationIp']):$($data['DestinationPort'])" } }
                11 {
                    if ($data['TargetFilename'] -and $data['TargetFilename'] -notlike 'C:\Windows\*') {
                        $fileWrites += $data['TargetFilename']
                    }
                }
                13 { if ($data['TargetObject']) { $registry += $data['TargetObject'] } }
            }
        }
    } else {
        $after = @(Get-Process)
        $processes = @($after | Where-Object { $baselineProcesses -notcontains $_.Id } |
            ForEach-Object { $_.ProcessName })
        foreach ($conn in @(Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
            $network += "$($conn.RemoteAddress):$($conn.RemotePort)"
        }
        foreach ($key in $runKeys) {
            if (-not (Test-Path $key)) { continue }
            $props = Get-ItemProperty -Path $key
            foreach ($p in $props.PSObject.Properties) {
                if ($p.Name -like 'PS*') { continue }
                if (-not $baselineRun.ContainsKey($p.Name) -or $baselineRun[$p.Name] -ne $p.Value) {
                    $registry += "$key\$($p.Name)"
                }
            }
        }
    }

    $completed = $true
} catch {
    $notes += "harness error: $($_.Exception.Message)"
} finally {
    Save-Report -Ok $completed
}
