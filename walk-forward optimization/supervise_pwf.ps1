# Supervisor for the cash-matched walk-forward.
#
# The machine is shared with another project's sweeps (~10 GB across 14 workers), and
# this stage has already been OOM-killed three times: silently, with a zero-byte log and
# no traceback, which is indistinguishable from a clean exit if you only watch the exit
# code. So completion is judged by the OUTPUT FILE existing, never by the process being
# gone, and a disappearance without that file is treated as a kill and retried.
#
# Writes one line per event to supervise_pwf.status so progress is readable at a glance.

$ErrorActionPreference = "Continue"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$py      = "C:\Users\Truong\Documents\work desk\quant python projects\stockhunt\.venv\Scripts\python.exe"
$outFile = Join-Path $here "results\portfolio_wf_cashmatch.csv"
$status  = Join-Path $here "logs\supervise_pwf.status"
$maxRestarts = 4
$restarts = 0

function Note($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Add-Content -Path $status -Value $line -Encoding utf8
}

function Running() {
    # Match on the command line, not a PID: a restart changes the PID and the wrapper
    # process shares the name.
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like "*portfolio_wf.py*--walkforward*" }).Count
}

function Launch() {
    $log = Join-Path $here "logs\pwf.log"
    Start-Process -FilePath $py `
        -ArgumentList "-u","portfolio_wf.py","--tf","1d","4h","--pit","--catalog",
                      "--walkforward","--out","portfolio_wf_cashmatch.csv" `
        -WorkingDirectory $here -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $here "logs\pwf.err") | Out-Null
}

Note "supervisor started; waiting on $outFile"
for ($i = 1; $i -le 240; $i++) {
    if (Test-Path $outFile) {
        Note ("DONE - output written, {0} bytes" -f (Get-Item $outFile).Length)
        break
    }
    $n = Running
    if ($n -eq 0) {
        if ($restarts -ge $maxRestarts) {
            Note "GAVE UP after $restarts restarts - output never appeared"
            break
        }
        $restarts++
        Note "process gone with no output -> assuming OOM kill, restart #$restarts"
        Launch
        Start-Sleep -Seconds 20
    }
    elseif ($i % 10 -eq 0) {
        $ram = [math]::Round((Get-Process python -ErrorAction SilentlyContinue |
                Measure-Object WorkingSet64 -Sum).Sum / 1MB)
        $picks = @(Get-ChildItem (Join-Path $here "results\pwf_picks_*.csv") -ErrorAction SilentlyContinue).Count
        Note ("alive; {0} panels have picks; all python RAM {1} MB" -f $picks, $ram)
    }
    Start-Sleep -Seconds 30
}
Note "supervisor exiting"
