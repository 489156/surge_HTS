# surge daily runner — invoked by Windows Task Scheduler.
#   morning (07:13 KST, Tue-Sat): ingest + score yesterday   (US session closed)
#   evening (21:35 KST, Mon-Fri): tonight's duel call        (Asia closed, US not open)
# Logs: data\logs\surge_<date>_<mode>.log

param(
    [ValidateSet("morning", "evening")]
    [string]$Mode = "morning"
)

$proj = "C:\Users\khy48\Downloads\vibe code\STOCK"
Set-Location $proj
$env:PYTHONIOENCODING = "utf-8"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }

$logDir = Join-Path $proj "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("surge_{0}_{1}.log" -f (Get-Date -Format "yyyy-MM-dd"), $Mode)

$script:failures = 0

function Invoke-Surge([string]$ArgsLine) {
    Add-Content -Path $log -Encoding UTF8 -Value `
        ("`n==== {0} :: surge {1} ====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $ArgsLine)
    # cmd handles native stderr redirection cleanly (PS 5.1 wraps it in errors)
    cmd /c "`"$uv`" run surge $ArgsLine >> `"$log`" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        $script:failures++
        Add-Content -Path $log -Encoding UTF8 -Value `
            ("!!!! FAILED (exit {0}) :: surge {1}" -f $LASTEXITCODE, $ArgsLine)
    }
}

if ($Mode -eq "morning") {
    # CRITICAL-FIRST ordering: the light duel loop (eval/archive/gap) runs before
    # the heavy surge research steps, so a timeout-kill (seen 2026-06-12: late
    # start + 2h limit died mid-`fade`) can never cost a scoring day again.
    Invoke-Surge "duel-eval"
    Invoke-Surge "duel-archive --period 3mo"   # incremental history archive
    Invoke-Surge "duel-gap"                    # prediction-vs-actual cause analysis
    Invoke-Surge "snapshot --fast"
    Invoke-Surge "backfill-outcomes"
    Invoke-Surge "fade"
    Invoke-Surge "stats"
    Invoke-Surge "report"      # one-screen digest (incl. variant A/B leaderboard)
} else {
    Invoke-Surge "duel-eval"              # catch-up scoring (idempotent)
    Invoke-Surge "duel --pair all"        # tonight's calls, all 4 pairs
}

Add-Content -Path $log -Encoding UTF8 -Value `
    ("`n==== {0} :: {1} done ({2} failures) ====" -f `
     (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Mode, $script:failures)
# propagate failure to Task Scheduler's "Last Run Result"
exit $(if ($script:failures -gt 0) { 1 } else { 0 })
