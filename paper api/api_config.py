"""Every tunable of the API in one place, with the reasoning attached.

Each value is overridable from the environment or `../.env.local`. The defaults are the
ones the desk runs with; the numbers that are security parameters say what they trade
against, because "make the code longer" and "give it a longer life" pull in opposite
directions and neither is obviously right without the other number beside it.
"""

from __future__ import annotations

import api_paths
from api_paths import env, env_flag, env_int

APP_NAME = env("API_APP_NAME", "Stockhunt Paper Desk")
API_VERSION = "0.1.0"

# ------------------------------------------------------------------ one-time passwords

# Six digits is a million possibilities. That is only safe next to the two limits below:
# five attempts against one live challenge is a 1-in-200,000 chance of a blind guess, and
# the challenge dies after ten minutes whether or not it is used.
OTP_LENGTH = env_int("API_OTP_LENGTH", 6)
OTP_TTL_SECONDS = env_int("API_OTP_TTL_SECONDS", 600)
OTP_MAX_ATTEMPTS = env_int("API_OTP_MAX_ATTEMPTS", 5)

# A resend invalidates the previous code, so an attacker cannot farm parallel challenges
# and multiply their attempts. The cooldown and the hourly caps exist for the other
# direction: this endpoint sends mail on an unauthenticated request, so without a limit it
# is a way to have Gmail deliver a hundred messages to somebody who never asked.
OTP_RESEND_COOLDOWN_SECONDS = env_int("API_OTP_RESEND_COOLDOWN", 60)
OTP_MAX_PER_HOUR_EMAIL = env_int("API_OTP_MAX_PER_HOUR_EMAIL", 5)
OTP_MAX_PER_HOUR_IP = env_int("API_OTP_MAX_PER_HOUR_IP", 20)

# ----------------------------------------------------------------------------- sessions

# 30 days, absolute. There is no refresh and no sliding window: a token that renews itself
# on use never expires for whoever is actually using it, which is also true of whoever
# stole it. Re-authenticating is one email.
SESSION_TTL_DAYS = env_int("API_SESSION_TTL_DAYS", 30)

# The browser's copy of that same session. One sessions table, two ways of carrying the
# token: a script sends `Authorization: Bearer`, a page cannot -- `<script src="app.js">`
# takes no headers, so gating the board needs a cookie or it needs nothing.
#
# HttpOnly, so a cross-site script cannot read it. SameSite=Lax, which is what stands in
# for a CSRF token here: it stops the cookie riding along on a POST or DELETE issued from
# somebody else's page, while still being sent on a top-level navigation to the board.
SESSION_COOKIE = env("API_SESSION_COOKIE", "sh_session")
COOKIE_PATH = "/"

# ------------------------------------------------------------------------------- server

HOST = env("API_HOST", "127.0.0.1")
PORT = env_int("API_PORT", 8080)

# Browsers only. A script calling this API is unaffected by CORS, so an empty list is the
# right default -- it is not a lock, it is a statement that no web origin is expected yet.
CORS_ORIGINS = [o.strip() for o in (env("API_CORS_ORIGINS", "") or "").split(",") if o.strip()]

# `X-Forwarded-For` is a request header, so anyone can write it. Honour it only when this
# process really does sit behind a proxy (a Cloudflare tunnel, nginx), because trusting it
# otherwise lets a caller pick their own rate-limit bucket and defeat the IP cap entirely.
TRUST_PROXY = env_flag("API_TRUST_PROXY", False)

# --------------------------------------------------------------------------------- mail

SMTP_HOST = env("API_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = env_int("API_SMTP_PORT", 465)
SMTP_TIMEOUT = env_int("API_SMTP_TIMEOUT", 20)
GMAIL_USER = env("GMAIL_USER")

# Whitespace stripped, and that is not tidiness — it is the difference between working and
# not. Google DISPLAYS an app password as four groups of four (`abcd efgh ijkl mnop`), so
# that is what everybody copies, but SMTP AUTH takes the sixteen characters and rejects
# the spaced form with a plain "Username and Password not accepted". The advice that
# follows that error is always "check your password", which is exactly what somebody who
# just pasted the right password will not find. Stripped once, here, rather than in
# `mailer` — every consumer of this value wants the same thing.
GMAIL_APP_PASSWORD = (env("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip() or None
MAIL_FROM = env("API_MAIL_FROM", GMAIL_USER)
MAIL_FROM_NAME = env("API_MAIL_FROM_NAME", APP_NAME)

# ---------------------------------------------------------------------------- dev modes

# Returns the code in the HTTP response instead of requiring a mailbox. It exists so the
# flow can be exercised end to end before the SMTP credentials are in place, and it is a
# complete bypass of authentication for anyone who can reach the port -- so it refuses to
# bind anywhere but loopback (`run_api.py`) and shouts on every startup.
DEV_ECHO_OTP = env_flag("API_DEV_ECHO_OTP", False)

# Seeded into an empty allowlist on first start, so a fresh install is not locked out of
# its own admin CLI. Only ever consulted when the table has no rows.
OWNER_EMAIL = env("API_OWNER_EMAIL")

# ---------------------------------------------------------------------------- the board

# Serve `../Stockhunt Dashboard/web/` behind the login. Off, and the process is a pure
# API; on, it is the only way the board reaches anyone who is not sitting at this machine,
# because `serve.py` now refuses to bind anything but loopback.
SERVE_BOARD = env_flag("API_SERVE_BOARD", True)

# How often a background thread rebuilds the research leaderboard, so that no reader has
# to. `0` turns it off and every reader pays for the first build after a restart.
#
# The board is a query over `results.db` and `board_rank` memoises it on the store's
# revision, so a warm board costs a dictionary lookup and a cold one costs the whole join
# -- 20.7s measured on the deployed box. Whoever reloads first after a deploy is the one
# who eats that today, and they eat it staring at a blank page, because `app.js` awaits
# `/v1/research/board` before its first render.
#
# Thirty seconds is chosen against what it is watching rather than against taste. A tick
# that finds the revision unchanged is one SELECT of one row, so the idle price is
# nothing; what it is waiting for is `research_worker.py` finishing a submission in
# ANOTHER process, and half a minute of lag on a job that took minutes to score is not
# perceptible to whoever submitted it.
BOARD_WARM_SECONDS = float(env("API_BOARD_WARM_SECONDS", "30") or 0)

# ------------------------------------------------------------------- the manager desk
#
# What one account may ask the desk to run. None of this is a risk limit — the desk trades
# no real money — it is a guard against one runaway script registering until the sandbox
# venue's account is meaningless and the board is unreadable.
#
# The desk enforces its own ceiling too (`desk_control.MAX_MEMBER_STRATEGIES`), and it is
# the one that binds: this process cannot see the book and must not be the only thing
# standing between a bug and the venue.
MAX_STRATEGIES_PER_ACCOUNT = env_int("API_MAX_STRATEGIES", 6)

# Fixed, not chosen by the caller. Equities round to whole shares, so a book has to be
# large enough that the rounding is a rounding rather than a decision — at $435 a slice a
# $570 share rounds 0.72 up to 1 and holds 131% of its capital. See CAPITAL_PER_SYSTEM in
# `run_paper.py`, which this deliberately matches so a member's book and a house leg's are
# directly comparable.
CAPITAL_PER_STRATEGY = float(env("API_CAPITAL_PER_STRATEGY", "10000") or 10000)

# Orders per minute, per account. A trading API's cheapest protection: the inbox is a
# database and a bot in a tight retry loop can fill it faster than the desk drains it.
MAX_ORDERS_PER_MINUTE = env_int("API_MAX_ORDERS_PER_MINUTE", 60)

# Research submissions per minute, per account. Two orders of magnitude below the order
# limit, because these are not cheap: each one is a walk-forward run over a whole sheet
# and the queue is drained one at a time. Filling it gains nobody anything.
MAX_TRIALS_PER_MINUTE = env_int("API_MAX_TRIALS_PER_MINUTE", 4)

# How old the desk's heartbeat may get before this process calls it down.
#
# The desk ticks every second (`desk_control.TICK_SECONDS`), so twenty is twenty missed
# passes — wide enough that a slow reconcile, a long drain or a moment of disk contention
# never reads as an outage, and narrow enough that a member watching a pending
# registration learns within seconds that nothing is going to happen.
#
# It is a DISPLAY threshold and nothing waits on it. Nothing here retries, refuses or
# escalates on a stale pulse: the ledger is still the only channel, and a heartbeat that
# could block a write would be a dependency of the API on the desk being up — the exact
# coupling this folder exists to avoid.
DESK_STALE_SECONDS = env_int("API_DESK_STALE_SECONDS", 20)

# Timeframes a registration may name. This must stay a subset of
# `paper_config.MEMBER_TIMEFRAMES` — the desk is what actually has to subscribe to a bar,
# and offering one it cannot feed means a `201` here followed by a rejection there, which
# is the failure the console was rebuilt to stop producing.
#
# It is restated rather than imported for the reason the whole folder exists: this process
# imports no trading code, so it cannot read `paper_config` without dragging the backtest
# engine into an HTTP server. Widen the desk first, then this.
#
# `1m` is on neither list. One poll task per subscription, aligned to the bar close, makes
# a minute book a different vendor-credit regime rather than a faster one.
TIMEFRAMES = tuple(
    t.strip() for t in env("API_TIMEFRAMES", "1d,4h,2h,1h,15m,5m").split(",") if t.strip())


def mail_configured() -> bool:
    return bool(GMAIL_USER and GMAIL_APP_PASSWORD)


def startup_banner() -> list[str]:
    """The lines printed at boot. Anything that silently weakens auth must appear here."""
    lines = [
        f"{APP_NAME} API v{API_VERSION}",
        f"  auth store   {api_paths.AUTH_DB}",
        f"  otp          {OTP_LENGTH} digits, {OTP_TTL_SECONDS // 60} min, "
        f"{OTP_MAX_ATTEMPTS} attempts",
        f"  sessions     {SESSION_TTL_DAYS} days, absolute",
        f"  mail         {'via ' + str(GMAIL_USER) if mail_configured() else 'NOT CONFIGURED'}",
        f"  cors         {', '.join(CORS_ORIGINS) if CORS_ORIGINS else 'no browser origins'}",
        f"  board        {'served at / behind the login' if SERVE_BOARD else 'not served'}",
        f"  board warmer {f'every {BOARD_WARM_SECONDS:g}s' if BOARD_WARM_SECONDS > 0 else 'off'}",
    ]
    if TRUST_PROXY:
        lines.append("  proxy        trusting X-Forwarded-For")
    if DEV_ECHO_OTP:
        lines.append("  !! API_DEV_ECHO_OTP is on: codes are returned in the response.")
        lines.append("  !! Anyone who can reach this port can sign in as any allowed user.")
    if not mail_configured() and not DEV_ECHO_OTP:
        lines.append("  !! No SMTP credentials: no code can be delivered and nobody can log in.")
        lines.append("  !! Set GMAIL_USER and GMAIL_APP_PASSWORD, or run mailer.py --check.")
    return lines
