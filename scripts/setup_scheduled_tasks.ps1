<#
  setup_scheduled_tasks.ps1 — enable the surge "improves on its own every day" routines
  in Windows Task Scheduler, so data is collected, scored, and self-improved daily
  without manual runs.

    Install : powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1
    Remove  : powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1 -Remove
    Verify  : Get-ScheduledTask -TaskName 'surge-*' | Select-Object TaskName,State

  Safety: these run YOUR program on YOUR machine; they only score / evolve hypotheses /
  judge / record. Nothing here trades, deploys, sends, or promotes a strategy to live —
  promotions stay human-approved (learn.gate + the approval queue). Tasks run while you
  are logged in; missed runs catch up via StartWhenAvailable. Fully reversible (-Remove).
#>
param([switch]$Remove)

$root  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$surge = Join-Path $root ".venv\Scripts\surge.exe"

# name ; weekdays ; HH:mm (KST/local) ; the surge command chain (cmd && cmd)
$tasks = @(
  @{ name = "surge-self-improve";  days = @("Tuesday","Wednesday","Thursday","Friday","Saturday"); at = "07:55"; chain = "daily" },
  @{ name = "surge-daily-evening"; days = @("Monday","Tuesday","Wednesday","Thursday","Friday");   at = "21:35"; chain = "duel-eval `&`& `"$surge`" duel --pair all" },
  @{ name = "surge-daily-morning"; days = @("Tuesday","Wednesday","Thursday","Friday","Saturday"); at = "07:13"; chain = "duel-eval `&`& `"$surge`" snapshot `&`& `"$surge`" report" },
  @{ name = "surge-kr-eod";        days = @("Monday","Tuesday","Wednesday","Thursday","Friday");   at = "16:05"; chain = "rotation-eval `&`& `"$surge`" rotation" }
)

foreach ($t in $tasks) {
  Unregister-ScheduledTask -TaskName $t.name -Confirm:$false -ErrorAction SilentlyContinue
}
if ($Remove) { Write-Host "Removed all surge-* routines."; return }

if (-not (Test-Path $surge)) {
  Write-Error "surge.exe not found at $surge - install the project (uv sync / pip install -e .) first."
  exit 1
}

foreach ($t in $tasks) {
  $argline = '/c "' + '"' + $surge + '" ' + $t.chain + '"'
  $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argline -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $t.days -At $t.at
  $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-ScheduledTask -TaskName $t.name -Action $action -Trigger $trigger -Settings $set `
    -Description "surge autonomous routine (self-improving stock analysis)" | Out-Null
  Write-Host ("registered {0,-20} {1} {2}" -f $t.name, $t.at, ($t.days -join ","))
}
Write-Host "`nDone. The program now runs daily on its own. Verify:"
Write-Host "  Get-ScheduledTask -TaskName 'surge-*' | Select-Object TaskName,State"
