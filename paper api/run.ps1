# Start the API, and with it the dashboard behind a login.
#
#   .\run.ps1                    # http://127.0.0.1:8080, local only
#   .\run.ps1 -Tunnel            # ...plus a public https://<random>.trycloudflare.com
#   .\run.ps1 -Port 9000
#   .\run.ps1 -Stop              # stop whatever this script started
#
# This is the launcher that replaced `..\Stockhunt Dashboard\run.ps1 -Tunnel`. The board it
# serves is the same `web/` directory, from the same files, with one difference: a reader
# has to prove they own an address on the allowlist before any of it is sent.
#
# It serves and it authenticates. It does not trade -- `..\paper trading engine\run_paper.py`
# is a separate process and stays separate, so if this dies the desk keeps trading.

[CmdletBinding()]
param(
    [int]$Port = 8080,
    [switch]$Tunnel,
    [switch]$Lan,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Here
$Logs = Join-Path $Here "logs"
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
# Shared with the dashboard's launcher rather than downloaded twice: it is a 52 MB binary
# and two copies would drift on version with nothing to notice it.
$Cloudflared = Join-Path $Repo "Stockhunt Dashboard\tools\cloudflared.exe"
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

# Refuse to expose a desk nobody can be invited to. An empty allowlist behind a tunnel --
# or on the LAN -- is a login screen that can never be passed, and, worse, it looks like it
# is working.
$users = & $Python (Join-Path $Here "admin_users.py") list
if (($Tunnel -or $Lan) -and ($users -join "`n") -notmatch "active") {
    throw "the allowlist is empty. Add yourself first:`n" +
          "    python admin_users.py allow you@example.com --admin"
}

# Bind to 0.0.0.0 when tunnelling, or when asked for the LAN. A plain local run has no
# reason to be on the network at all, so that stays the default.
#
# `-Lan` is for reaching the board from a phone or a second machine on your own network.
# It is NOT the same thing as `-Tunnel`: there is no TLS, so the session cookie travels in
# clear over the LAN and anyone on that network can read it off the wire. That is a real
# trade and it is the reason this is an explicit switch rather than a bind address people
# pass by habit. Use `-Tunnel` for anything beyond your own household.
$BindHost = if ($Tunnel -or $Lan) { "0.0.0.0" } else { "127.0.0.1" }
$pids = @()

# Behind the tunnel the hop to uvicorn is plain HTTP and only `X-Forwarded-Proto` says the
# reader is on https -- which is what decides whether the session cookie is marked Secure,
# and what the rate limiter buckets on. Set only when there really is a proxy in front,
# because trusting those headers without one lets any caller forge both.
$env:API_TRUST_PROXY = if ($Tunnel) { "1" } else { "0" }

$api = Start-Process -FilePath $Python `
    -ArgumentList @("-u", "run_api.py", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Here -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "run.log") `
    -RedirectStandardError  (Join-Path $Logs "run.err")
$pids += "$($api.Id) run_api.py"
"run_api.py  pid $($api.Id)  ->  http://127.0.0.1:$Port"

if ($Lan -and -not $Tunnel) {
    $ips = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }
    foreach ($ip in $ips) { "            on the LAN  ->  http://$($ip.IPAddress):$Port" }
    ""
    "  On the LAN there is no TLS, so the session cookie travels in clear."
    "  Fine on a network you control; use -Tunnel for anything else."
}

if ($Tunnel) {
    if (-not (Test-Path $Cloudflared)) {
        throw "no cloudflared at $Cloudflared - download it from github.com/cloudflare/cloudflared"
    }
    # Quick tunnel: no Cloudflare account, new random hostname every start. It terminates
    # TLS, which is what makes the page https:// -- and that matters more here than it did
    # for the old open server, because a session cookie on a plain-http public URL is a
    # session anyone on the path can lift.
    $tun = Start-Process -FilePath $Cloudflared `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port",
                        "--no-autoupdate", "--protocol", "quic") `
        -WorkingDirectory $Here -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "tunnel.log") `
        -RedirectStandardError  (Join-Path $Logs "tunnel.err")
    $pids += "$($tun.Id) cloudflared"
    "cloudflared pid $($tun.Id)  ->  public URL appears in logs\tunnel.err in a few seconds"
    ""
    "Whoever you send it to needs an invitation first:"
    "    python admin_users.py allow them@example.com"
}

$pids | Set-Content -Path $PidFile -Encoding utf8
""
"stop with: .\run.ps1 -Stop"
