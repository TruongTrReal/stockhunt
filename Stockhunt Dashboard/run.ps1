# Start the dashboard: local server, plus an optional public tunnel.
#
# There was no launcher before this -- serve.py and cloudflared were started by hand and
# the only record of how was the argument list in logs/tunnel.err. This is that, written
# down.
#
#   .\run.ps1                    # http://127.0.0.1:8765, local only
#   .\run.ps1 -Tunnel            # ...plus a public https://<random>.trycloudflare.com
#   .\run.ps1 -Port 9000
#   .\run.ps1 -Stop              # stop whatever this script started
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
$Cloudflared = Join-Path $Here "tools\cloudflared.exe"
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

# Bind to 0.0.0.0 only when tunnelling; a local-only run has no reason to be on the LAN.
$BindHost = if ($Tunnel) { "0.0.0.0" } else { "127.0.0.1" }
$pids = @()

$serve = Start-Process -FilePath $Python `
    -ArgumentList @("serve.py", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Here -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "serve.log") `
    -RedirectStandardError  (Join-Path $Logs "serve.err")
$pids += "$($serve.Id) serve.py"
"serve.py    pid $($serve.Id)  ->  http://127.0.0.1:$Port"

if ($Tunnel) {
    if (-not (Test-Path $Cloudflared)) {
        throw "no cloudflared at $Cloudflared - download it from github.com/cloudflare/cloudflared"
    }
    # Quick tunnel: no Cloudflare account, new random hostname every start. It terminates
    # TLS, which is what makes the page https:// and lets app.js upgrade its socket to
    # wss:// on the same origin -- the whole reason serve.py carries /ws itself.
    $tun = Start-Process -FilePath $Cloudflared `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port",
                        "--no-autoupdate", "--protocol", "quic") `
        -WorkingDirectory $Here -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "tunnel.log") `
        -RedirectStandardError  (Join-Path $Logs "tunnel.err")
    $pids += "$($tun.Id) cloudflared"
    "cloudflared pid $($tun.Id)  ->  public URL appears in logs\tunnel.err in a few seconds"
}

$pids | Set-Content -Path $PidFile -Encoding utf8
""
"stop with: .\run.ps1 -Stop"
