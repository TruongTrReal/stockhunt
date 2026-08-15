"""The auth layer, end to end, against a throwaway database.

Run from this directory::

    ..\\.venv\\Scripts\\python -m pytest test_auth.py -q

What is worth testing here is not "does a valid code work" — it is the set of things that
would each silently turn the allowlist into a formality:

* an unregistered address getting a *different answer* from a registered one,
* a code surviving its expiry, its attempt budget, or a newer code,
* a token outliving the account that justified it.

The environment is set before the application modules are imported, because `api_paths`
resolves the state directory at import time and pointing it at a temporary directory is
the whole isolation mechanism.
"""

from __future__ import annotations

import os
import tempfile

os.environ["STOCKHUNT_API_STATE"] = tempfile.mkdtemp(prefix="stockhunt-api-test-")
os.environ["API_SERVER_SECRET"] = "secret-for-tests-only"
os.environ["API_DEV_ECHO_OTP"] = "0"
os.environ["API_OWNER_EMAIL"] = ""

import pytest                                                    # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

import api_app                                                   # noqa: E402
import api_auth                                                  # noqa: E402
import api_config                                                # noqa: E402
import authdb                                                    # noqa: E402
import mailer                                                    # noqa: E402

ALLOWED = "trader@example.com"
STRANGER = "nobody@example.com"


@pytest.fixture()
def sent(monkeypatch):
    """Capture what would have been mailed, and send nothing."""
    box: list[tuple[str, str]] = []
    monkeypatch.setattr(mailer, "send_code",
                        lambda to, code, ttl: box.append((to, code)))
    return box


@pytest.fixture()
def client(sent):
    authdb.connect()
    for table in ("sessions", "otp_challenges", "users", "audit"):
        authdb.connect().execute(f"DELETE FROM {table}")
    authdb.allow(ALLOWED, label="test")
    with TestClient(api_app.app) as c:
        yield c


def _code(box) -> str:
    assert box, "no code was sent"
    return box[-1][1]


def _sign_in(client, box, email: str = ALLOWED) -> str:
    client.post("/auth/otp", json={"email": email})
    r = client.post("/auth/session", json={"email": email, "code": _code(box)})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ------------------------------------------------------------------- the happy path

def test_sign_in_and_use_the_token(client, sent):
    token = _sign_in(client, sent)
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == ALLOWED


def test_email_is_case_and_space_insensitive(client, sent):
    r = client.post("/auth/otp", json={"email": f"  {ALLOWED.upper()}  "})
    assert r.status_code == 202
    assert sent and sent[-1][0] == ALLOWED


# ----------------------------------------------------- the allowlist is not discoverable

def test_stranger_gets_the_same_answer_and_no_mail(client, sent):
    known = client.post("/auth/otp", json={"email": ALLOWED})
    sent.clear()
    unknown = client.post("/auth/otp", json={"email": STRANGER})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()      # identical body, not merely a similar one
    assert sent == []
    assert authdb.sends_since(STRANGER, authdb.ago(3600)) == 0


def test_revoked_user_gets_the_stranger_treatment(client, sent):
    authdb.revoke(ALLOWED)
    sent.clear()
    r = client.post("/auth/otp", json={"email": ALLOWED})
    assert r.status_code == 202 and sent == []


def test_stranger_cannot_verify(client, sent):
    r = client.post("/auth/session", json={"email": STRANGER, "code": "123456"})
    assert r.status_code == 401


def test_the_throttled_reply_is_the_same_reply(client, sent, monkeypatch):
    """A 429 only real users can trigger would leak exactly what the 202 hides."""
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 3600)
    first = client.post("/auth/otp", json={"email": ALLOWED})
    sent.clear()
    second = client.post("/auth/otp", json={"email": ALLOWED})
    stranger = client.post("/auth/otp", json={"email": STRANGER})

    assert second.status_code == 202
    assert second.json() == first.json() == stranger.json()
    assert sent == [], "the cooldown must suppress the mail, not the response"


# ------------------------------------------------------------------------ the code itself

def test_wrong_code_is_rejected(client, sent):
    client.post("/auth/otp", json={"email": ALLOWED})
    wrong = "0" * api_config.OTP_LENGTH
    if wrong == _code(sent):                                  # 1 in a million, but still
        wrong = "1" * api_config.OTP_LENGTH
    r = client.post("/auth/session", json={"email": ALLOWED, "code": wrong})
    assert r.status_code == 401


def test_code_is_single_use(client, sent):
    token = _sign_in(client, sent)
    assert token
    again = client.post("/auth/session", json={"email": ALLOWED, "code": _code(sent)})
    assert again.status_code == 401


def test_attempts_are_capped_and_the_real_code_dies_with_them(client, sent):
    client.post("/auth/otp", json={"email": ALLOWED})
    real = _code(sent)
    wrong = "9999999999"[:api_config.OTP_LENGTH]
    if wrong == real:
        wrong = "1111111111"[:api_config.OTP_LENGTH]
    for _ in range(api_config.OTP_MAX_ATTEMPTS):
        assert client.post("/auth/session",
                           json={"email": ALLOWED, "code": wrong}).status_code == 401
    burned = client.post("/auth/session", json={"email": ALLOWED, "code": real})
    assert burned.status_code == 401, "the attempt budget must burn the challenge"


def test_expired_code_is_rejected(client, sent):
    client.post("/auth/otp", json={"email": ALLOWED})
    authdb.connect().execute(
        "UPDATE otp_challenges SET expires_at = ? WHERE email = ?",
        (authdb.ago(60), ALLOWED))
    r = client.post("/auth/session", json={"email": ALLOWED, "code": _code(sent)})
    assert r.status_code == 401


def test_a_new_code_retires_the_old_one(client, sent, monkeypatch):
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 0)
    client.post("/auth/otp", json={"email": ALLOWED})
    first = _code(sent)
    client.post("/auth/otp", json={"email": ALLOWED})
    second = _code(sent)
    assert first != second
    assert client.post("/auth/session",
                       json={"email": ALLOWED, "code": first}).status_code == 401
    assert client.post("/auth/session",
                       json={"email": ALLOWED, "code": second}).status_code == 200


def test_hourly_send_cap_stops_the_mail(client, sent, monkeypatch):
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(api_config, "OTP_MAX_PER_HOUR_EMAIL", 2)
    for _ in range(2):
        client.post("/auth/otp", json={"email": ALLOWED})
    sent.clear()
    r = client.post("/auth/otp", json={"email": ALLOWED})
    assert r.status_code == 202 and sent == []


def test_ip_cap_is_the_one_limit_that_answers_back(client, sent, monkeypatch):
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(api_config, "OTP_MAX_PER_HOUR_IP", 2)
    for _ in range(2):
        client.post("/auth/otp", json={"email": ALLOWED})
    r = client.post("/auth/otp", json={"email": ALLOWED})
    assert r.status_code == 429


# ------------------------------------------------------------------------------ sessions

def test_no_token_no_entry(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me",
                      headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_logout_kills_the_token(client, sent):
    token = _sign_in(client, sent)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.delete("/auth/session", headers=auth).status_code == 204
    assert client.get("/auth/me", headers=auth).status_code == 401


def test_logout_everywhere(client, sent, monkeypatch):
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 0)
    first = _sign_in(client, sent)
    second = _sign_in(client, sent)
    r = client.delete("/auth/sessions", headers={"Authorization": f"Bearer {second}"})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {first}"}).status_code == 401


def test_revoking_a_user_invalidates_a_live_token(client, sent):
    """The property `revoke` exists for: no window between losing access and being told."""
    token = _sign_in(client, sent)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=auth).status_code == 200
    authdb.revoke(ALLOWED)
    assert client.get("/auth/me", headers=auth).status_code == 401


def test_session_list_marks_the_current_one(client, sent, monkeypatch):
    monkeypatch.setattr(api_config, "OTP_RESEND_COOLDOWN_SECONDS", 0)
    _sign_in(client, sent)
    token = _sign_in(client, sent)
    rows = client.get("/auth/sessions",
                      headers={"Authorization": f"Bearer {token}"}).json()
    assert len(rows) == 2
    assert sum(r["current"] for r in rows) == 1


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@c.com", "@example.com"])
def test_rubbish_addresses_are_refused(client, bad):
    assert client.post("/auth/otp", json={"email": bad}).status_code == 422


def test_non_numeric_code_is_refused(client):
    r = client.post("/auth/session", json={"email": ALLOWED, "code": "abcdef"})
    assert r.status_code == 422


# ------------------------------------------------------------------------------- storage

def test_the_code_is_not_stored_in_the_clear(client, sent):
    client.post("/auth/otp", json={"email": ALLOWED})
    code = _code(sent)
    rows = authdb.connect().execute("SELECT code_hash FROM otp_challenges").fetchall()
    assert rows and all(code not in r["code_hash"] for r in rows)


def test_the_token_is_not_stored_in_the_clear(client, sent):
    token = _sign_in(client, sent)
    rows = authdb.connect().execute("SELECT token_hash FROM sessions").fetchall()
    assert rows and all(r["token_hash"] != token for r in rows)


def test_failures_are_audited(client, sent):
    client.post("/auth/otp", json={"email": STRANGER})
    events = [r["event"] for r in authdb.recent_audit(20)]
    assert "otp.unknown_email" in events


def test_a_session_without_an_email_is_refused_not_a_500():
    """A credential that cannot say who it belongs to has not authenticated.

    Every endpoint behind `current_session` indexes `row["email"]` and hands it to
    `normalize_email`, which calls `.strip()`. A single such row turned a routine
    `GET /auth/keys` into a bare "Internal error" — and because the console reloads its
    panels after every action, that error appeared against whatever had just been clicked
    rather than against the request that actually failed.
    """
    from fastapi import HTTPException
    import api_auth as _auth

    class _Req:
        cookies = {api_config.SESSION_COOKIE: "whatever"}
        headers = {}

    original = authdb.session
    authdb.session = lambda token_hash, touch=True: {"id": 7, "email": None,
                                                     "is_admin": 0, "account_id": "01"}
    try:
        with pytest.raises(HTTPException) as caught:
            _auth.current_session(_Req(), None)
        assert caught.value.status_code == 401
    finally:
        authdb.session = original
