# Start the dashboard locally. Sharing it now lives in `..\paper api\run.ps1`.
#
# There was no launcher before this -- serve.py and cloudflared were started by hand and
# the only record of how was the argument list in logs/tunnel.err. This is that, written
# down.
#
#   .\run.ps1                    # http://127.0.0.1:8765, local only
#   .\run.ps1 -Port 9000
#   .\run.ps1 -Stop              # stop whatever this script started
#
# `-Tunnel` is gone. It published a server with no login, so the URL was the whole
# security model: anyone it reached, or anyone it was forwarded to, saw every position and
# every result. The same board is now served behind an emailed sign-in code by
# `..\paper api\run.ps1 -Tunnel`, from the same `web/` directory and the same files.
#
# This serves. It does not trade. `../paper trading engine/run_paper.py` is a separate
# process and stays separate -- if the server dies the desk keeps trading.

[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$Tunnel,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Here "logs"
$Python = Join-Path (Split-Path -Parent $Here) ".venv\Scripts\python.exe"
# cloudflared still lives under this folder's tools\ and is still the binary that gets
# downloaded here; `..\paper api\run.ps1` reaches across for it rather than keeping a
# second 52 MB copy.
$PidFile = Join-Path $Logs "run.pids"

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

if ($Stop) {
    if (-not (Test-Path $PidFile)) { "nothing to stop (no $PidFile)"; return }
    foreach ($line in Get-Content $PidFile) {
        $procId, $what = $line -split "\s+", 2
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $procId -Force; "stopped $procId ($what)" }
        else { "already gone: $procId ($what)" }
    }
    Remove-Item $PidFile -Force
    return
}

if (-not (Test-Path $Python)) { throw "no venv python at $Python" }

if ($Tunnel) {
    throw @"
-Tunnel has moved. serve.py has no login, so tunnelling it published the whole desk to
whoever had the URL. The same board, behind an emailed sign-in code:

    cd "..\paper api"
    python admin_users.py allow them@example.com
    .\run.ps1 -Tunnel
"@
}

# serve.py refuses anything but loopback, so there is nothing else this can be.
$BindHost = "127.0.0.1"
$pids = @()

$serve = Start-Process -FilePath $Python `
    -ArgumentList @("serve.py", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Here -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "serve.log") `
    -RedirectStandardError  (Join-Path $Logs "serve.err")
$pids += "$($serve.Id) serve.py"
"serve.py    pid $($serve.Id)  ->  http://127.0.0.1:$Port"

$pids | Set-Content -Path $PidFile -Encoding utf8
""
"stop with: .\run.ps1 -Stop"
