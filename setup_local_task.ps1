# ============================================================================
#  Registers (or re-registers) the Windows Task Scheduler job that runs
#  run_all_local.py every 15 minutes, indefinitely.
#    powershell -ExecutionPolicy Bypass -File setup_local_task.ps1 [-Disabled]
#  - Runs as the current user, interactive logon only (no stored password).
#  - Allowed on battery, keeps running if power is unplugged, no time limit.
#  - Skips a run if the previous one is still going (no overlapping writes).
# ============================================================================
param([string]$TaskName = 'LA Heat Outage Scrapers', [switch]$Disabled)

$runner = Join-Path $PSScriptRoot 'run_all_local.py'
# pythonw.exe has no console, so no window flashes up every 15 minutes.
$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$runner`"" -WorkingDirectory $PSScriptRoot

# Start at the next quarter hour, repeat every 15 min with no end.
$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes(15 * [math]::Ceiling(($now.Minute + 1) / 15))
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 15)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'LA Heat 2026: scrape 8 LA/OC utility outage feeds every 15 min (sce-outage-tracker).' `
    -Force | Out-Null
if ($Disabled) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }

Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime | Format-List
(Get-ScheduledTask -TaskName $TaskName).State
