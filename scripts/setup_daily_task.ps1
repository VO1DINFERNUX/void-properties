# Registers (or re-registers) the Windows Task Scheduler job that fires the
# void-properties daily pipeline every morning at 7:00 AM.
#
# Usage (from an elevated or normal PowerShell -- registering a task for your
# own user account doesn't require admin):
#     powershell -ExecutionPolicy Bypass -File scripts\setup_daily_task.ps1
#
# Idempotent: if a task by this name already exists, it's unregistered first
# and recreated -- so re-running this after editing the trigger/action below
# updates the existing job instead of erroring or duplicating it.
#
# What it points at: scripts\run_daily_pipeline.ps1 -- a thin wrapper (see
# that file's own header comment for why it exists rather than pointing
# Task Scheduler straight at python.exe) that runs daily_pipeline.py and
# appends timestamped output to logs\daily_pipeline.log.
#
# To check on it later:      Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
# To run it on demand:       Start-ScheduledTask -TaskName $TaskName
# To remove it entirely:     Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

$TaskName    = "VoidProperties-DailyPipeline"
$RepoRoot    = Split-Path -Parent $PSScriptRoot
$WrapperPath = Join-Path $RepoRoot "scripts\run_daily_pipeline.ps1"

if (-not (Test-Path $WrapperPath)) {
    throw "Can't find $WrapperPath -- run this from the void-properties repo (scripts\setup_daily_task.ps1)."
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WrapperPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At 7:00AM

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Found an existing '$TaskName' task -- unregistering it first so this run replaces it cleanly."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description ("Runs void-properties' daily lead pipeline (scrape -> resolve -> enrich -> " +
                  "score+MAO -> hot-lead alerts) every morning at 7 AM. " +
                  "See scripts/daily_pipeline.py for what each run does, and " +
                  "logs/daily_pipeline.log for what actually happened on a given morning.") `
    | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName' -- runs daily at 7:00 AM."
Write-Host "  Action:  powershell.exe -File `"$WrapperPath`""
Write-Host "  Logs:    $RepoRoot\logs\daily_pipeline.log"
Write-Host ""
Write-Host "Check on it:   Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Run it now:    Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it:     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
