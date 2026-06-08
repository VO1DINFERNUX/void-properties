# Wrapper Windows Task Scheduler actually invokes for the 7 AM daily pipeline.
#
# Why a wrapper at all, instead of pointing the task straight at python.exe:
# Task Scheduler runs with no console and a minimal environment -- nothing
# captures stdout/stderr unless something redirects it, so a failure five
# stages in would otherwise vanish into nothing. This pins down a real
# python.exe (Task Scheduler doesn't reliably resolve the WindowsApps
# execution-alias shim `python` resolves to interactively), runs
# daily_pipeline.py from the repo root so its own relative paths resolve,
# and appends everything -- including stderr -- to logs\daily_pipeline.log
# with a timestamped run header/footer so a quiet morning and a broken one
# are equally easy to tell apart later.
#
# See scripts/setup_daily_task.ps1 for how this gets registered, and the
# README's "Daily pipeline" section for how to check on / change / remove it.

$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir   = Join-Path $repoRoot "logs"
$logFile  = Join-Path $logDir "daily_pipeline.log"
$python   = "C:\Users\Fernu\AppData\Local\Python\pythoncore-3.14-64\python.exe"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$started = Get-Date
"`r`n===== run started  $($started.ToString('yyyy-MM-dd HH:mm:ss')) =====" |
    Out-File -FilePath $logFile -Append -Encoding utf8

Push-Location $repoRoot
try {
    & $python "scripts\daily_pipeline.py" *>> $logFile
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

$finished = Get-Date
"===== run finished $($finished.ToString('yyyy-MM-dd HH:mm:ss')) (exit=$exitCode, $([int]($finished - $started).TotalSeconds)s) =====" |
    Out-File -FilePath $logFile -Append -Encoding utf8
