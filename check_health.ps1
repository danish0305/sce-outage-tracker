# ============================================================================
#  Health check for the local LA/OC scraper job. Run anytime:
#    powershell -ExecutionPolicy Bypass -File check_health.ps1
#  Reports, per utility, the last SUCCESS time from run_log.txt and warns when it
#  is older than 1 hour (or missing). Also checks the scheduled task is enabled,
#  since tasks have gone silently dormant before.
# ============================================================================
param([int]$MaxAgeMinutes = 60, [string]$TaskName = 'LA Heat Outage Scrapers')

$Log = Join-Path $PSScriptRoot 'run_log.txt'
$Utilities = 'SCE', 'SDGE', 'LADWP', 'GLENDALE', 'AZUSA', 'BURBANK', 'PASADENA', 'ANAHEIM'
$now = Get-Date
$problems = 0

# ---- scheduled task state ----
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "WARNING  Scheduled task '$TaskName' not found." -ForegroundColor Red; $problems++
} else {
    $info = $task | Get-ScheduledTaskInfo
    $color = if ($task.State -eq 'Disabled') { 'Red' } else { 'Green' }
    Write-Host ("Task '{0}': {1} | last run {2} (result {3}) | next run {4}" -f `
        $TaskName, $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime) -ForegroundColor $color
    if ($task.State -eq 'Disabled') { $problems++ }
}
Write-Host ''

# ---- per-utility last success / last failure ----
$lines = if (Test-Path $Log) { Get-Content $Log -Encoding utf8 } else { @() }
$last = @{}
foreach ($l in $lines) {
    if ($l -match '^(\S+)\s+(SUCCESS|FAILURE)\s+\[([A-Z]+)\]') {
        $t = [datetimeoffset]::Parse($Matches[1]).LocalDateTime
        $last["$($Matches[3])|$($Matches[2])"] = $t
    }
}

'{0,-9} {1,-20} {2,-9} {3}' -f 'UTILITY', 'LAST SUCCESS', 'AGE', 'STATUS' | Write-Host
foreach ($u in $Utilities) {
    $s = $last["$u|SUCCESS"]; $f = $last["$u|FAILURE"]
    if (-not $s) {
        $row = '{0,-9} {1,-20} {2,-9} {3}' -f $u, 'never', '-', 'WARNING: no successful run logged'
        Write-Host $row -ForegroundColor Red; $problems++; continue
    }
    $age = $now - $s
    $ageTxt = if ($age.TotalHours -ge 1) { '{0:N1}h' -f $age.TotalHours } else { '{0:N0}m' -f $age.TotalMinutes }
    $status = 'OK'; $color = 'Green'
    if ($age.TotalMinutes -gt $MaxAgeMinutes) { $status = "WARNING: older than $MaxAgeMinutes min"; $color = 'Red'; $problems++ }
    if ($f -and $f -gt $s) { $status += " (last run FAILED at $($f.ToString('yyyy-MM-dd HH:mm')))"; if ($color -eq 'Green') { $color = 'Yellow' } }
    Write-Host ('{0,-9} {1,-20} {2,-9} {3}' -f $u, $s.ToString('yyyy-MM-dd HH:mm'), $ageTxt, $status) -ForegroundColor $color
}
Write-Host ''
if ($problems) { Write-Host "$problems problem(s) found." -ForegroundColor Red; exit 1 }
Write-Host 'All 8 utilities healthy.' -ForegroundColor Green; exit 0
