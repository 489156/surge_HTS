# surge KR rotation runner — invoked by Windows Task Scheduler after KRX close.
#   16:05 KST (Mon-Fri): score prior calls (T+1/T+3/T+5) + variant A/B, then
#   generate + store next-session candidates. KRX closes 15:30 KST.
# Logs: data\logs\surge_kr_<date>.log

$proj = "C:\Users\khy48\Downloads\vibe code\STOCK"
Set-Location $proj
$env:PYTHONIOENCODING = "utf-8"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }

$logDir = Join-Path $proj "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("surge_kr_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$failures = 0

function Invoke-Surge([string]$ArgsLine) {
    Add-Content -Path $log -Encoding UTF8 -Value `
        ("`n==== {0} :: surge {1} ====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $ArgsLine)
    cmd /c "`"$uv`" run surge $ArgsLine >> `"$log`" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        $script:failures++
        Add-Content -Path $log -Encoding UTF8 -Value ("!!!! FAILED (exit {0})" -f $LASTEXITCODE)
    }
}

Invoke-Surge "rotation-eval"          # score prior calls + variant leaderboard
Invoke-Surge "rotation --why"         # next-session candidates (stored)

Add-Content -Path $log -Encoding UTF8 -Value `
    ("`n==== {0} :: kr done ({1} failures) ====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $failures)
exit $(if ($failures -gt 0) { 1 } else { 0 })
