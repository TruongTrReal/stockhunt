"""The authentication layer: request a code by email, trade it for a session token.

There is **no registration endpoint**, and that is the design rather than a missing
feature. An address can only get in because the owner ran `admin_users.py allow`, so the
job of this module is to prove that whoever is calling controls a mailbox already on that
list — nothing more.

Two properties are load-bearing and easy to break by accident:

**`POST /auth/otp` answers identically for every address.** Same status, same body, same
shape, whether the address is on the allowlist, was revoked yesterday, or has never been
seen. The endpoint is unauthenticated and reachable by anyone with the URL, so any
difference in its reply is a way to ask "is this person on the desk" and get an answer.
That is why the per-email limits below are enforced by *not sending mail* rather than by
returning 429 — a 429 that only real users can trigger is the same oracle wearing a
different status code.

**Mail is sent off the request path.** A `BackgroundTasks` job, not an `await` inside the
handler. Latency is the small half; the real reason is that an SMTP round trip takes about
a second and doing it inline would make the allowlisted branch measurably slower than the
other one, which restores by stopwatch exactly the leak the identical body was there to
close. The residual difference — a few SQLite writes — is orders of magnitude below what
is measurable across a network.

Failures therefore cannot be reported in the response. They land in the audit table as
`otp.send_failed` and in the log; `admin_users.py audit` is where to look when somebody
says no code arrived.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Request,
                     Response, status)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

import api_config
import api_paths
import authdb
import mailer

log = logging.getLogger("stockhunt.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Deliberately conservative, and deliberately not RFC 5322. The full grammar admits
# quoted local parts and bracketed literals that no mail this desk sends will ever go to,
# and every one of them is an extra shape to reason about in a header.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}$")

_bearer = HTTPBearer(auto_error=False, description="The token from POST /auth/session")


# ------------------------------------------------------------------------------- models

class OtpRequest(BaseModel):
    email: str = Field(..., max_length=320, examples=["you@example.com"])

    @field_validator("email")
    @classmethod
    def _valid(cls, v: str) -> str:
        v = authdb.normalize_email(v)
        if not EMAIL_RE.match(v):
            raise ValueError("not an email address")
        return v


class VerifyRequest(OtpRequest):
    code: str = Field(..., min_length=4, max_length=12, examples=["123456"])

    @field_validator("code")
    @classmethod
    def _digits(cls, v: str) -> str:
        v = v.strip().replace(" ", "").replace("-", "")
        if not v.isdigit():
            raise ValueError("the code is digits only")
        return v


class OtpResponse(BaseModel):
    status: str = "sent"
    message: str
    expires_in: int
    # Present only under API_DEV_ECHO_OTP, which refuses to run off loopback.
    dev_code: str | None = None


class UserOut(BaseModel):
    email: str
    label: str | None = None
    is_admin: bool = False
    last_login_at: str | None = None


class SessionOut(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserOut


class BrowserSessionOut(BaseModel):
    """What the login page gets: who you are, and nothing it could leak.

    The token is deliberately absent. It goes out as an HttpOnly cookie, which means the
    page's own JavaScript cannot read it either — so a script injected into the board has
    no way to lift the session and carry it off the machine.
    """
    user: UserOut
    expires_at: str


class SessionInfo(BaseModel):
    created_at: str
    expires_at: str
    last_used_at: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    current: bool = False


# ------------------------------------------------------------------------------ helpers

def client_ip(request: Request) -> str:
    """The address the rate limiter buckets on.

    `X-Forwarded-For` is only consulted when `API_TRUST_PROXY` says this process really is
    behind one. Reading it unconditionally would let any caller set their own bucket by
    sending a header, which turns the IP cap into a formality.
    """
    if api_config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hash_code(email: str, code: str) -> str:
    """HMAC, keyed on the server secret — see the module docstring in `authdb`."""
    msg = f"{authdb.normalize_email(email)}:{code}".encode("utf-8")
    return hmac.new(api_paths.server_secret(), msg, hashlib.sha256).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_code() -> str:
    """Digits from `secrets`, leading zeros kept.

    `randbelow(10**n)` formatted with zero padding is the same thing, but this way the
    length is one constant and there is no 10**n that quietly loses precision if somebody
    sets `API_OTP_LENGTH` to something silly.
    """
    return "".join(secrets.choice("0123456789") for _ in range(api_config.OTP_LENGTH))


def _deliver(email: str, code: str, ip: str) -> None:
    """The background job. Swallows nothing, reports everything to the audit table."""
    try:
        mailer.send_code(email, code, api_config.OTP_TTL_SECONDS)
        authdb.audit("otp.sent", email, ip)
    except mailer.MailError as exc:
        log.error("OTP delivery failed for %s: %s", email, exc)
        authdb.audit("otp.send_failed", email, ip, str(exc))


def _email_limited(email: str) -> str | None:
    """Should this address be sent another code right now? Returns why not, or None.

    Enforced by silence, never by a status code. See the module docstring.
    """
    last = authdb.last_send_at(email)
    if last and last > authdb.ago(api_config.OTP_RESEND_COOLDOWN_SECONDS):
        return "cooldown"
    if authdb.sends_since(email, authdb.ago(3600)) >= api_config.OTP_MAX_PER_HOUR_EMAIL:
        return "hourly_cap"
    return None


# ---------------------------------------------------------------------------- endpoints

@router.post("/otp", response_model=OtpResponse, status_code=status.HTTP_202_ACCEPTED,
             summary="Ask for a sign-in code")
def request_otp(body: OtpRequest, request: Request, tasks: BackgroundTasks) -> OtpResponse:
    """Send a one-time code, if — and only if — the address is on the allowlist.

    The reply does not say which happened.
    """
    ip = client_ip(request)
    email = body.email

    # The one limit that DOES change the response, and the one that can: it is scoped to
    # the caller's address, not to the mailbox they are asking about, so tripping it
    # reveals nothing about who is registered.
    if authdb.ip_sends_since(ip, authdb.ago(3600)) >= api_config.OTP_MAX_PER_HOUR_IP:
        authdb.audit("otp.ip_limited", email, ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many sign-in requests from this address. "
                                   "Try again in an hour.")

    reply = OtpResponse(
        message="If that address is registered, a sign-in code is on its way.",
        expires_in=api_config.OTP_TTL_SECONDS,
    )

    user = authdb.active_user(email)
    if user is None:
        # Recorded, because a stream of these is somebody probing the allowlist and the
        # audit table is the only place that would ever show it.
        authdb.audit("otp.unknown_email", email, ip)
        return reply

    limited = _email_limited(email)
    if limited:
        authdb.audit(f"otp.throttled.{limited}", email, ip)
        return reply

    code = _new_code()
    authdb.create_challenge(email, _hash_code(email, code),
                            api_config.OTP_TTL_SECONDS, ip)
    authdb.audit("otp.requested", email, ip)
    tasks.add_task(_deliver, email, code, ip)

    if api_config.DEV_ECHO_OTP:
        log.warning("DEV_ECHO_OTP: code for %s is %s", email, code)
        reply.dev_code = code
    return reply


def _authenticate(body: VerifyRequest, request: Request) -> tuple[dict, str, dict]:
    """Spend the code and open a session. Returns `(user, token, session row)`.

    Every failure — unknown address, wrong code, expired code, too many attempts — leaves
    here as the same 401 with the same wording. The audit table keeps the distinction; the
    caller does not get it.
    """
    ip = client_ip(request)
    email = body.email
    fail = HTTPException(status.HTTP_401_UNAUTHORIZED,
                         detail="That code is not valid. Request a new one.")

    user = authdb.active_user(email)
    if user is None:
        authdb.audit("login.unknown_email", email, ip)
        raise fail

    ok, reason = authdb.verify(email, _hash_code(email, body.code),
                               api_config.OTP_MAX_ATTEMPTS)
    if not ok:
        authdb.audit(f"login.failed.{reason}", email, ip)
        raise fail

    token = secrets.token_urlsafe(32)
    row = authdb.create_session(email, _hash_token(token), api_config.SESSION_TTL_DAYS,
                                ip, request.headers.get("user-agent"))
    authdb.mark_login(email)
    return dict(user), token, row


def _https(request: Request) -> bool:
    """Is this request on a secure origin, from this process's point of view?

    Behind the tunnel the connection to *uvicorn* is plain HTTP and only the forwarded
    header says otherwise, so a cookie marked Secure unconditionally would never be set
    locally and one never marked Secure would travel in clear on the LAN. The scheme is
    read per response instead of configured once.
    """
    if api_config.TRUST_PROXY:
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            return proto.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


def public_base_url(request: Request) -> str:
    """The origin a caller reached this process on, as it should be written down.

    The agent brief and the connect step put a base URL into code somebody will run, so
    getting the scheme wrong there is not cosmetic — it is a doc that names `http://` for
    a desk that is only reachable over `https://`. Same per-request reading as the cookie's
    `Secure` flag, and for the same reason, so there is one answer to "what am I publicly".

    The `Host` header is caller-controlled, which is harmless here: the result goes back to
    the caller who sent it and nowhere else.
    """
    host = request.headers.get("host") or request.url.netloc
    return f"{'https' if _https(request) else 'http'}://{host}"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        api_config.SESSION_COOKIE, token,
        max_age=api_config.SESSION_TTL_DAYS * 86400,
        httponly=True,                       # unreadable from JavaScript
        samesite="lax",                      # this is the CSRF defence; see api_config
        secure=_https(request),
        path=api_config.COOKIE_PATH,
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(api_config.SESSION_COOKIE, path=api_config.COOKIE_PATH,
                           httponly=True, samesite="lax", secure=_https(request))


@router.post("/session", response_model=SessionOut,
             summary="Trade a code for a token (scripts)")
def create_session(body: VerifyRequest, request: Request) -> SessionOut:
    """The token in the response body, for callers that can send a header."""
    user, token, row = _authenticate(body, request)
    authdb.audit("login.ok", body.email, client_ip(request), "bearer")
    return SessionOut(
        token=token,
        expires_at=row["expires_at"],
        user=UserOut(email=body.email, label=user["label"],
                     is_admin=bool(user["is_admin"]),
                     last_login_at=user["last_login_at"]),
    )


@router.post("/browser", response_model=BrowserSessionOut,
             summary="Trade a code for a cookie (the login page)")
def create_browser_session(body: VerifyRequest, request: Request,
                           response: Response) -> BrowserSessionOut:
    """The same session, delivered as an HttpOnly cookie instead.

    Two endpoints rather than a flag on one, because they hand out the same secret under
    materially different terms — one to a script that will store it somewhere, one to a
    browser that must not be able to read it. A boolean in a request body is too quiet a
    place to record that difference.
    """
    user, token, row = _authenticate(body, request)
    set_session_cookie(response, request, token)
    authdb.audit("login.ok", body.email, client_ip(request), "cookie")
    return BrowserSessionOut(
        user=UserOut(email=body.email, label=user["label"],
                     is_admin=bool(user["is_admin"]),
                     last_login_at=user["last_login_at"]),
        expires_at=row["expires_at"],
    )


# ---------------------------------------------------------------------- the dependency

def session_token(request: Request,
                  creds: HTTPAuthorizationCredentials | None = None) -> str | None:
    """The token on this request: the bearer header first, the cookie second.

    Header first so that a script talking to a browser-signed-in machine gets the identity
    it asked for rather than whichever one the browser happens to be holding.
    """
    if creds is not None and creds.credentials:
        return creds.credentials
    return request.cookies.get(api_config.SESSION_COOKIE)


def session_for_token(token: str | None, touch: bool = True) -> dict | None:
    """Resolve a raw token to its session row. The only caller that has one in hand is
    the WebSocket handler, which reads the cookie off the handshake rather than a
    `Request`."""
    return authdb.session(_hash_token(token), touch=touch) if token else None


def optional_session(request: Request) -> dict | None:
    """The session row, or None. For handlers that redirect rather than refuse."""
    token = request.cookies.get(api_config.SESSION_COOKIE)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip() or token
    return session_for_token(token)


def current_session(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Resolve the request to a session row, or 401.

    This is the seam every future endpoint hangs off — the strategy CRUD, the portfolio
    views, anything that belongs to one user. It returns the whole row rather than just an
    email so those endpoints can see `is_admin` and the token hash without a second query.
    """
    unauth = HTTPException(status.HTTP_401_UNAUTHORIZED,
                           detail="Sign in first: POST /auth/otp, then POST /auth/session.",
                           headers={"WWW-Authenticate": "Bearer"})
    token = session_token(request, creds)
    if not token:
        raise unauth
    row = authdb.session(_hash_token(token))
    if row is None:
        raise unauth
    # A session that resolves but carries no email is not a usable identity. It should be
    # unreachable — `sessions.email` is NOT NULL and the query joins `users` on it — but
    # every endpoint downstream indexes this row and passes the value straight into
    # `normalize_email`, which does `.strip()` on it. One such row turned a routine
    # `GET /auth/keys` into a bare 500, and because the console reloads its panels after
    # every action, the error surfaced against whatever the reader had just clicked.
    #
    # 401 rather than 500: a credential that cannot say who it belongs to is a credential
    # that has not authenticated, and it tells the caller to sign in again — which fixes
    # it — instead of "Internal error", which does not.
    if not row.get("email"):
        log.error("session %s resolved with no email; refusing it", row.get("id"))
        raise unauth
    return row


def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Who is calling, by either credential. The seam every `/v1` endpoint hangs off.

    Two audiences reach this API and they cannot share one credential. A browser signs in
    with an emailed code and carries a cookie; a manager's strategy runs unattended on
    their own machine and can complete no email flow, so it carries an API key.

    Routing is by PREFIX, not by trying both: a key is `sk_live_…` and a session token is
    not, so one lookup answers instead of two. That also means a mistyped key can never be
    silently probed against the sessions table.

    Returns the shape every downstream endpoint actually needs — crucially `account_id`,
    which is the identity that leaves this process. The email never does.
    """
    unauth = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Sign in first (POST /auth/otp, then POST /auth/session), or send an "
               "API key as `Authorization: Bearer sk_live_...`.",
        headers={"WWW-Authenticate": "Bearer"})

    token = session_token(request, creds)
    if not token:
        raise unauth

    if token.startswith(authdb.KEY_PREFIX):
        row = authdb.api_key(authdb.hash_key(token))
        via = "key"
    else:
        row = authdb.session(_hash_token(token))
        via = "session"
    if row is None:
        raise unauth

    account = row.get("account_id")
    if not account:
        # Every allowlisted address is assigned one at `allow()` time and the migration
        # backfills the rest, so this is unreachable — and if it ever is reached, refusing
        # beats handing back a principal with no identity for the desk to key on.
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="This account has no account id; contact the owner.")
    return {"email": row["email"], "account_id": account,
            "is_admin": bool(row["is_admin"]), "via": via}


def current_user(session: dict = Depends(current_session)) -> UserOut:
    return UserOut(email=session["email"], label=session["label"],
                   is_admin=bool(session["is_admin"]))


def require_admin(session: dict = Depends(current_session)) -> dict:
    """For endpoints that manage other people. The CLI is the other way in."""
    if not session["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Admins only.")
    return session


# -------------------------------------------------------------------- session lifecycle

@router.get("/me", response_model=UserOut, summary="Who this token belongs to")
def whoami(session: dict = Depends(current_session)) -> UserOut:
    user = authdb.user(session["email"]) or {}
    return UserOut(email=session["email"], label=session["label"],
                   is_admin=bool(session["is_admin"]),
                   last_login_at=user.get("last_login_at"))


@router.get("/sessions", response_model=list[SessionInfo],
            summary="Every live token on this account")
def list_sessions(session: dict = Depends(current_session)) -> list[SessionInfo]:
    return [SessionInfo(created_at=s["created_at"], expires_at=s["expires_at"],
                        last_used_at=s["last_used_at"], ip=s["ip"],
                        user_agent=s["user_agent"],
                        current=s["token_hash"] == session["token_hash"])
            for s in authdb.sessions_for(session["email"])]


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(request: Request, response: Response,
           session: dict = Depends(current_session)) -> None:
    authdb.revoke_session(session["token_hash"])
    clear_session_cookie(response, request)
    authdb.audit("logout", session["email"], client_ip(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT,
             summary="Sign out (for a browser form)")
def logout_post(request: Request, response: Response,
                session: dict = Depends(current_session)) -> None:
    """The same thing over POST.

    `fetch` can send a DELETE, so this is not for capability — it is for the day the sign
    out is a `<form>`, which can only issue GET or POST. A GET is not an option: anything
    that changes state on a GET can be triggered by an image tag on another page.
    """
    logout(request, response, session)


@router.delete("/sessions", summary="Sign out everywhere")
def logout_all(request: Request, response: Response,
               session: dict = Depends(current_session)) -> dict:
    n = authdb.revoke_sessions(session["email"])
    clear_session_cookie(response, request)
    authdb.audit("logout.all", session["email"], client_ip(request), f"{n} revoked")
    return {"revoked": n}


# --------------------------------------------------------------------------- api keys
#
# Keys are minted and revoked BEHIND THE BROWSER LOGIN and never by a key. That is the
# whole containment: a stolen key can trade a manager's paper book, which is bad, but it
# cannot mint a second key that survives revoking the first — so revocation is final
# rather than a race against whoever holds the credential.


class KeyRequest(BaseModel):
    label: str | None = Field(None, max_length=80,
                              examples=["mean-reversion bot, laptop"])


class KeyOut(BaseModel):
    """A key as it is listed: identifiable, and useless to anybody who reads it."""
    id: int
    label: str | None = None
    prefix: str
    created_at: str
    last_used_at: str | None = None


class NewKeyOut(KeyOut):
    """The one response that carries the secret. It is never retrievable again."""
    key: str
    warning: str = ("Store this now — it is not saved anywhere and cannot be shown "
                    "again. If you lose it, revoke it and make another.")


@router.post("/keys", response_model=NewKeyOut, status_code=status.HTTP_201_CREATED,
             summary="Mint an API key for a strategy that runs unattended")
def create_key(body: KeyRequest, request: Request,
               session: dict = Depends(current_session)) -> NewKeyOut:
    raw, row = authdb.create_api_key(session["email"], body.label)
    authdb.audit("key.created", session["email"], client_ip(request),
                 f"id={row['id']} label={body.label!r}")
    return NewKeyOut(id=row["id"], label=row["label"], prefix=row["prefix"],
                     created_at=row["created_at"], key=raw)


@router.get("/keys", response_model=list[KeyOut], summary="Your live API keys")
def list_keys(session: dict = Depends(current_session)) -> list[KeyOut]:
    return [KeyOut(**k) for k in authdb.api_keys_for(session["email"])]


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke one API key")
def delete_key(key_id: int, request: Request,
               session: dict = Depends(current_session)) -> None:
    if not authdb.revoke_api_key(session["email"], key_id):
        # 404 rather than 403 for a key belonging to somebody else: telling a caller that
        # an id exists but is not theirs is an answer to a question they should not be
        # able to ask.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such key.")
    authdb.audit("key.revoked", session["email"], client_ip(request), f"id={key_id}")
