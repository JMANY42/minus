param([int]$TimeoutSeconds = 180, [switch]$ReviewMode, [string]$KeepFile = '')

# Runs inside the guest. Collects evidence, then writes exactly one report.
# Every failure is caught and still produces a report with completed=false: a
# silent exit would look like a detonation that observed nothing, which is the
# one outcome that must never be manufactured.
#
# Review mode is for files that are not Windows programs -- a movie, an image,
# a subtitle. The host attaches them on a read-only CD labelled MINUS_REVIEW;
# this script copies them to a writable input directory and opens each one with
# the Windows application a person would use. The point is to exercise the same
# parser path a player would, while the same process/network/file/registry
# telemetry watches for actions the file was not supposed to take.

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
$opened = 0
$graveyarded = 0
$keptTotal = 0
$keptOpened = 0
$vlcPath = ''
$partial = $false
$completed = $false
$start = Get-Date
$keepNames = @()
if ($KeepFile -and (Test-Path $KeepFile)) {
    $keepNames = @(Get-Content -Path $KeepFile | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $keptTotal = $keepNames.Count
}

function Save-Report {
    param([bool]$Ok)
    $payload = [ordered]@{
        completed          = $Ok
        artifacts_launched = $launched
        artifacts_opened   = $opened
        artifacts_graveyarded = $graveyarded
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

function Get-OpenTarget {
    # An explicit handler is better than a shell association: it is present in
    # the image, it is deterministic, and an association prompt would count as
    # "opened" without a parser ever running.
    param([string]$Extension)

    $media = @('mp4', 'mkv', 'avi', 'mov', 'wmv', 'm4v', 'webm', 'mpg', 'mpeg', 'ts', 'flv')
    $images = @('jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff')
    $text = @('txt', 'srt', 'vtt', 'nfo', 'md', 'log', 'ini', 'cfg')

    if ($media -contains $Extension) {
        if ($script:vlcPath) { return $script:vlcPath }
        $wmp = 'C:\Program Files\Windows Media Player\wmplayer.exe'
        $wmp86 = 'C:\Program Files (x86)\Windows Media Player\wmplayer.exe'
        if (Test-Path $wmp) { return $wmp }
        if (Test-Path $wmp86) { return $wmp86 }
        return ''
    }
    if ($images -contains $Extension) {
        $paint = Join-Path $env:WINDIR 'System32\mspaint.exe'
        if (Test-Path $paint) { return $paint }
        return ''
    }
    if ($text -contains $Extension) {
        $notepad = Join-Path $env:WINDIR 'System32\notepad.exe'
        if (Test-Path $notepad) { return $notepad }
        return ''
    }
    return ''
}

try {
    New-Item -ItemType Directory -Force -Path $workDir | Out-Null

    $sourceDir = $inputDir
    if ($ReviewMode) {
        # Find the read-only review CD by its volume label. Open from the CD
        # directly: copying a multi-gigabyte movie into the overlay is slow
        # enough to outlive the WinRM command that started the harness. VLC,
        # Paint and Notepad all read from a CD; only their cache writes go to
        # the writable overlay.
        $reviewDrive = $null
        foreach ($vol in @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue)) {
            if ($vol.VolumeName -eq 'MINUS_REVIEW') { $reviewDrive = $vol.DeviceID; break }
        }
        if ($null -eq $reviewDrive) {
            foreach ($vol in @(Get-Volume -ErrorAction SilentlyContinue)) {
                if ($vol.FileSystemLabel -eq 'MINUS_REVIEW' -and $vol.DriveLetter) {
                    $reviewDrive = "$($vol.DriveLetter):"
                    break
                }
            }
        }
        if ($null -eq $reviewDrive) {
            $notes += 'review CD MINUS_REVIEW not found'
            $partial = $true
        } else {
            $sourceDir = "$reviewDrive\"
            $notes += "review CD $reviewDrive"
        }
    }

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

    $vlc = Get-ChildItem -Path 'C:\tools\vlc' -Filter 'vlc.exe' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $vlc) { $vlcPath = $vlc.FullName; $notes += "media handler: $($vlc.FullName)" }
    elseif ($ReviewMode) { $notes += 'VLC not installed: media uses Windows Media Player' }

    # Defender scans without remediation so the sample and its verdict survive.
    $mp = Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe'
    if (Test-Path $mp) {
        & $mp -Scan -ScanType 3 -File $sourceDir -DisableRemediation | Out-Null
        $defender = @(Get-MpThreatDetection -ErrorAction SilentlyContinue |
            ForEach-Object { $_.ThreatName } | Select-Object -Unique)
    } else {
        $notes += 'MpCmdRun.exe not found'
    }

    $launchable = @('exe', 'com', 'bat', 'cmd', 'ps1', 'scr', 'lnk', 'msi')
    $files = @(Get-ChildItem -Path $sourceDir -Recurse -File -ErrorAction SilentlyContinue)
    # A movie needs enough seconds for the player to open the container; a
    # large torrent needs the total to stay bounded. The floor is five seconds
    # and the ceiling is the caller's whole budget.
    $perFile = [Math]::Max(5, [Math]::Min(30, [int]($TimeoutSeconds / [Math]::Max(1, $files.Count))))
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    foreach ($file in $files) {
        $ext = $file.Extension.TrimStart('.').ToLowerInvariant()
        $isLaunch = $launchable -contains $ext
        if (-not $isLaunch -and -not $ReviewMode) { continue }

        $remaining = [int]($deadline - (Get-Date)).TotalSeconds
        if ($remaining -le 1) {
            $partial = $true
            $notes += 'time budget exhausted before every file was opened'
            break
        }
        $wait = [Math]::Min($perFile, $remaining)

        $stem = [IO.Path]::GetFileNameWithoutExtension($file.Name)
        $dir = Join-Path $workDir $stem
        New-Item -ItemType Directory -Force -Path $dir | Out-Null

        $target = ''
        if ($isLaunch) {
            $target = $file.FullName
        } elseif ($ReviewMode) {
            $target = Get-OpenTarget $ext
        }

        $p = $null
        $explicitHandler = ($target -ne '') -and (-not $isLaunch)
        try {
            if ($target -ne '') {
                if ($isLaunch) {
                    $p = Start-Process -FilePath $target -WorkingDirectory $dir -PassThru -ErrorAction Stop
                } else {
                    $argList = @("`"$($file.FullName)`"")
                    if ($target -like '*vlc.exe') {
                        $argList = @(
                            '--play-and-exit',
                            '--no-video-title-show',
                            '--no-qt-privacy-ask',
                            '--no-qt-updates-notif',
                            '--no-qt-error-dialogs',
                            '--quiet',
                            "`"$($file.FullName)`""
                        )
                    }
                    $p = Start-Process -FilePath $target -ArgumentList $argList `
                        -WorkingDirectory $dir -PassThru -ErrorAction Stop
                    $notes += "opened $($file.Name) with $(Split-Path $target -Leaf)"
                }
            } else {
                # No known in-box handler: start the shell association but do
                # not count it. On Windows 10 this is often the "how do you
                # want to open this file?" dialog, which is not a parser and
                # must never manufacture a detonation.
                $p = Start-Process -FilePath $file.FullName -WorkingDirectory $dir -PassThru -ErrorAction Stop
                $notes += "shell association for $($file.Name) (not counted)"
            }
        } catch {
            $notes += "could not open $($file.Name): $($_.Exception.Message)"
        }

        if ($null -ne $p -and $explicitHandler) {
            # A handler that exits immediately (unsupported codec, corrupt
            # container) did not open the file. Give it a short grace period;
            # Notepad, Paint and WMP all stay alive past it.
            Start-Sleep -Seconds 2
            if ($p.HasExited) {
                $notes += "$($file.Name): handler exited without opening it"
                $p = $null
            }
        }
        if ($null -ne $p) {
            if ($isLaunch) { $launched++ }
            elseif ($explicitHandler) { $opened++ }
            if ($keepNames -contains $file.Name) { $keptOpened++ }
        }
        if ($null -ne $p -and -not $p.WaitForExit($wait * 1000)) {
            taskkill /T /F /PID $p.Id | Out-Null
            $notes += "killed $($file.Name) after ${wait}s"
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

    if ($keepNames.Count -gt 0 -and $keptOpened -lt $keptTotal) {
        $partial = $true
        $notes += "kept files opened $keptOpened of $keptTotal"
    }

    if ($ReviewMode -and $keepNames.Count -gt 0) {
        # The host selected what is worth keeping. Everything else is release
        # furniture or a second payload: collect the small ones in the guest's
        # graveyard. The source is a read-only CD, so this copies rather than
        # moves; a large non-kept file is left on the CD instead of doubling
        # the torrent into the overlay.
        $graveyardDir = Join-Path $root 'graveyard'
        New-Item -ItemType Directory -Force -Path $graveyardDir | Out-Null
        $graveyardMax = 64MB
        foreach ($file in @(Get-ChildItem -Path $sourceDir -Recurse -File -ErrorAction SilentlyContinue)) {
            if ($keepNames -contains $file.Name) { continue }
            if ($file.Length -gt $graveyardMax) {
                $notes += "not copied to graveyard (large): $($file.Name)"
                continue
            }
            Copy-Item -Path $file.FullName -Destination $graveyardDir -Force -ErrorAction SilentlyContinue
            if (Test-Path (Join-Path $graveyardDir $file.Name)) { $graveyarded++ }
        }
    }

    $completed = -not $partial
} catch {
    $notes += "harness error: $($_.Exception.Message)"
} finally {
    Save-Report -Ok $completed
}
