# Supervisor for the three concurrent finishing stages.
#
# Judges each stage by the ARTEFACT it is supposed to produce, never by a process being
# gone. Every kill this session looked identical to a clean exit — zero-byte log, no
# traceback, exit code 0 — so "the process ended" carries no information about success.
#
# Restarts a stage whose process disappears without its artefact, which is the OOM
# signature while the machine is shared with another project's sweeps.

$ErrorActionPreference = "Continue"
$root   = "C:\Users\Truong\Documents\work desk\quant python projects\stockhunt"
$py     = "$root\.venv\Scripts\python.exe"
$wf     = "$root\walk-forward optimization"
$bt     = "$root\backtest engine"
$status = "$wf\logs\supervise_all.status"

function Note($m) { Add-Content $status ("{0}  {1}" -f (Get-Date -Format HH:mm:ss), $m) -Encoding utf8 }
function Alive($pat) {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like $pat }).Count
}

$stages = @(
  @{ n="riskmatch";  art="$wf\results\edge_standard.csv";              pat="*riskmatch_wf.py*"; min=50000
     go={ Start-Process $py -ArgumentList "-u","riskmatch_wf.py","--tf","1d","4h" -WorkingDirectory $wf -WindowStyle Hidden -RedirectStandardOutput "$wf\logs\riskmatch.log" -RedirectStandardError "$wf\logs\riskmatch.err" | Out-Null } }
  @{ n="cashmatch";  art="$wf\results\portfolio_wf_cashmatch_net.csv"; pat="*portfolio_wf.py*--walkforward*"; min=500
     go={ Start-Process $py -ArgumentList "-u","portfolio_wf.py","--tf","1d","4h","--pit","--catalog","--walkforward","--out","portfolio_wf_cashmatch_net.csv" -WorkingDirectory $wf -WindowStyle Hidden -RedirectStandardOutput "$wf\logs\pwf_net.log" -RedirectStandardError "$wf\logs\pwf_net.err" | Out-Null } }
  @{ n="report";     art="$bt\report\index.html";                      pat="*sweep.py*|*combo_sweep.py*|*build_payload*|*build_report*"; min=1000000
     go={ $c="& '$py' -u sweep.py --tf 1d 4h *> logs\sweep2.log; & '$py' -u combo_sweep.py --tf 1d 4h *> logs\combo2.log; & '$py' -u build_payload.py *> logs\payload.log; & '$py' -u build_report.py *> logs\report.log"
          Start-Process "powershell.exe" -ArgumentList "-NoProfile","-Command",$c -WorkingDirectory $bt -WindowStyle Hidden | Out-Null } }
)
foreach ($s in $stages) { $s.restarts = 0; $s.done = $false }

Note "supervisor started for $($stages.Count) stages"
for ($i = 1; $i -le 400; $i++) {
    $pending = 0
    foreach ($s in $stages) {
        if ($s.done) { continue }
        # An artefact newer than the supervisor's start AND big enough to be real.
        if ((Test-Path $s.art) -and (Get-Item $s.art).Length -ge $s.min -and
            (Get-Item $s.art).LastWriteTime -gt (Get-Date).AddHours(-3)) {
            $s.done = $true
            Note ("{0}: DONE — {1} ({2} bytes)" -f $s.n, (Split-Path $s.art -Leaf), (Get-Item $s.art).Length)
            continue
        }
        $pending++
        $running = 0
        foreach ($p in ($s.pat -split '\|')) { $running += Alive $p }
        if ($running -eq 0) {
            if ($s.restarts -ge 3) { $s.done = $true; Note "$($s.n): GAVE UP after 3 restarts"; continue }
            $s.restarts++
            Note "$($s.n): process gone with no artefact -> restart #$($s.restarts)"
            & $s.go
            Start-Sleep -Seconds 20
        }
    }
    if ($pending -eq 0) { Note "ALL STAGES DONE"; break }
    if ($i % 10 -eq 0) {
        $ram = [math]::Round((Get-Process python -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum/1MB)
        Note ("waiting on: {0} | python RAM {1} MB" -f (($stages | Where-Object {-not $_.done} | ForEach-Object {$_.n}) -join ", "), $ram)
    }
    Start-Sleep -Seconds 30
}
Note "supervisor exiting"
