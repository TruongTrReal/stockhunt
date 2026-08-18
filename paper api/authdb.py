"""`state/auth.db` — who is allowed in, and who is currently in.

Four tables and one idea: **the allowlist is the user table.** There is no registration
endpoint anywhere in this service. A row in `users` exists because the owner ran
`admin_users.py allow`, and an email with no row gets the same reply as an email with one
— a 202 and no message — so the API cannot be used to discover who is registered.

Three things are hashed rather than stored, and each is hashed for a different reason:

* **The OTP** is stored as an HMAC under `api_paths.server_secret()`. Six digits is a
  million possibilities, so a plain SHA-256 of the code would be a lookup table; keying
  it means a stolen database is useless without the secret, which lives in a different
  file.
* **The session token** is stored as a plain SHA-256. It is 256 bits of `secrets` output,
  so there is no dictionary to run against it and the slow-hash argument that applies to
  passwords does not apply here — while a fast hash matters, because it runs on every
  authenticated request.
* **Nothing else.** Emails are stored in the clear: the whole point of the table is to be
  able to tell the owner who is on it.

Times are ISO-8601 UTC strings, as in the paper desk's `store.py`. They are fixed width
and end in `+00:00`, so lexicographic comparison is chronological comparison and expiry
can be a `WHERE` clause rather than a round trip through Python.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import api_paths

_lock = threading.RLock()

# One connection per thread — see `connect()` for why a shared one silently corrupts reads
# on a credential store. `_open` lets `close()` reach the ones this thread did not create;
# `_generation` is what `use()` bumps to invalidate them.
_local = threading.local()
# Keyed by thread ident, and holding the Thread so liveness can be asked. It was a plain
# list, and that leaked a file descriptor per short-lived caller: `_local` drops its
# reference when a thread exits, but the list kept a strong one forever, so the connection
# was never collected and its fd never closed. `sqlite3.Connection` cannot be weak-
# referenced, so the registry has to be pruned rather than made weak — see `_reap`.
#
# This is the SAME bug `stockhunt/deskdb.py` fixed, and the same fix; it is written twice
# because the two stores share no code. It is the BOARD that hits this one. FastAPI runs
# every sync endpoint on an anyio worker thread, those expire after ~10s idle, and a page
# left open polls `index.html` once a minute — so a quiet board gets a fresh thread, and
# therefore a fresh connection, almost every request. The API reached the 1024-fd soft
# limit in about seven hours of ordinary traffic, after which every request that had to
# open a file — including serving the board's own `index.html` — failed with
# `OSError: [Errno 24]` while systemd still reported the unit healthy and nginx served
# 500s. Measured on the box 2026-08-18: 1023 of 1024 fds, 10.5 hours after a restart.
#
# It does not reproduce on Windows, which has no comparable per-process fd ceiling — so
# this is a bug the dev box structurally cannot show you.
_open: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
_generation = 0
_db_path: Path = api_paths.AUTH_DB

SCHEMA = """
-- The allowlist. `active = 0` rather than a delete keeps the audit trail readable: a
-- revoked account's past events still name somebody the table can identify.
--
-- `account_id` is this address's identity EVERYWHERE OUTSIDE THIS DATABASE. Two
-- characters, and it is what the trading engine, the order ledger and every published
-- document carry instead of an email — so the desk can run somebody's book without ever
-- holding a piece of personal data. The mapping lives here and only here.
--
-- Declared without UNIQUE and given a unique INDEX below instead: SQLite cannot
-- `ALTER TABLE ADD COLUMN` with a UNIQUE constraint, and this column has to be addable to
-- an allowlist that already exists.
CREATE TABLE IF NOT EXISTS users (
    email         TEXT PRIMARY KEY,
    account_id    TEXT,
    label         TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- One row per code sent. Kept after use because the send history IS the rate limiter,
-- and because "when did somebody last try to log in as me" is a question worth being
-- able to answer.
CREATE TABLE IF NOT EXISTS otp_challenges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,
    ip          TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    email        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT,
    ip           TEXT,
    user_agent   TEXT
);

-- API keys, for the half of the audience that is not a browser.
--
-- A manager's strategy runs unattended on their own machine and cannot complete an email
-- code flow, so sessions — which come from a six-digit code and expire in thirty days —
-- are the wrong credential for it. A key is created in the browser BEHIND that login, so
-- every key still traces back to an address the owner personally allowlisted.
--
-- Stored as a plain SHA-256, deliberately, for the same reason session tokens are: the
-- secret is 256 bits of `secrets` output, so there is no dictionary to run and the
-- slow-hash argument does not apply — while the fast-hash argument does, since this runs
-- on every authenticated request a bot makes.
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT NOT NULL UNIQUE,
    email        TEXT NOT NULL,
    label        TEXT,
    prefix       TEXT NOT NULL,          -- the visible head, so a key is identifiable
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- Webhook secrets, for the half of the audience that cannot send a header at all.
--
-- TradingView posts a JSON body to a URL and offers no way to add one, so the only place a
-- credential can travel is the body — which is also a place credentials get pasted into
-- chat logs, screenshotted, and exported with the alert. So this is deliberately NOT the
-- account key: it is scoped to ONE strategy, it can only submit orders for that strategy,
-- and rotating it costs that one alert rather than every integration on the account.
--
-- Keyed on `account_id` rather than `email`, unlike `api_keys`. A webhook belongs to a
-- strategy, a strategy belongs to an account, and an account may have several sign-in
-- addresses; keying it on whichever address happened to mint it would kill a live alert
-- the day that address was retired. `webhook_secret()` requires SOME active user on the
-- account instead, which is the same protection at the right granularity.
--
-- One live secret per strategy at a time: minting again revokes the previous one, so
-- "rotate" is a single act rather than a list somebody has to prune. The revoked rows stay
-- for the audit trail.
CREATE TABLE IF NOT EXISTS webhook_secrets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    secret_hash  TEXT NOT NULL UNIQUE,
    account_id   TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    prefix       TEXT NOT NULL,          -- the visible head, so a leak is identifiable
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- Append-only. Every auth decision lands here, successful or not, because the failures
-- are the interesting half.
CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    event  TEXT NOT NULL,
    email  TEXT,
    ip     TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS ix_otp_email  ON otp_challenges(email, id);
CREATE INDEX IF NOT EXISTS ix_otp_ip     ON otp_challenges(ip, created_at);
CREATE INDEX IF NOT EXISTS ix_sess_email ON sessions(email, expires_at);
CREATE INDEX IF NOT EXISTS ix_audit_ts   ON audit(ts);
CREATE INDEX IF NOT EXISTS ix_keys_email ON api_keys(email, revoked_at);
CREATE INDEX IF NOT EXISTS ix_whk_strategy ON webhook_secrets(strategy_id, revoked_at);
CREATE INDEX IF NOT EXISTS ix_whk_account  ON webhook_secrets(account_id, revoked_at);
"""

# Runs after `_migrate`, because it indexes a column that migration may have just added.
#
# NOT unique, deliberately. One account may have SEVERAL sign-in addresses — the same
# person with a work and a personal mailbox, who wants one book and not two. They share an
# `account_id`, so they share strategies, orders, fills and the whole record; only the
# credential differs, and revoking one address leaves the other working.
#
# Uniqueness used to be the guard against two people's books silently merging. What
# replaces it is that merging is now an explicit, named act — `admin_users.py link a b` —
# rather than something a bug can do by accident: `next_account_id` still allocates one
# past the highest ever issued, so nothing collides on its own.
POST_MIGRATION = """
DROP INDEX IF EXISTS ix_users_account;
CREATE INDEX IF NOT EXISTS ix_users_account_id ON users(account_id);
"""

# `00` is the house — the desk's own rules, run off its own walk-forward sheets. It is
# never handed to a person, so allocation starts at `01`.
HOUSE_ACCOUNT = "00"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _encode_account(n: int) -> str:
    """Base-36, at least two characters. 1 -> '01', 35 -> '0z', 36 -> '10'.

    Two characters is 1,296 accounts and costs three of the 36 characters Nautilus allows
    in an `order_id_tag`. Past that it widens rather than wrapping — a desk with 1,296
    managers on it has better problems, and silently reusing an id would merge two
    people's books.
    """
    out = ""
    while n:
        n, rem = divmod(n, 36)
        out = _ALPHABET[rem] + out
    return (out or "0").rjust(2, "0")


def _decode_account(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 36 + _ALPHABET.index(ch)
    return n


def next_account_id(conn: sqlite3.Connection) -> str:
    """The next free id: one past the highest ever issued.

    Highest-ever rather than lowest-free, deliberately. Reusing the id of a purged account
    would silently re-point their strategies, fills and curve at whoever came next — the
    trading engine keys on the id alone and cannot tell that the person behind it changed.
    """
    used = [r[0] for r in conn.execute(
        "SELECT account_id FROM users WHERE account_id IS NOT NULL").fetchall()]
    highest = max((_decode_account(a) for a in used), default=0)
    return _encode_account(max(highest + 1, 1))


# --------------------------------------------------------------------------------- time

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plus(seconds: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def ago(seconds: float) -> str:
    """The timestamp `seconds` in the past — the left edge of a rate-limit window."""
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------- connection

def _reap() -> None:
    """Close the connections of threads that have exited. Caller must hold `_lock`.

    This is the whole fix for the descriptor leak described above `_open`. It runs from
    `connect()` and therefore only on a cache miss — which, for the caller that leaks, is
    exactly once per new thread, so the registry stays the size of the live thread count
    instead of growing without bound.
    """
    for ident, (thread, conn) in list(_open.items()):
        if thread.is_alive():
            continue
        try:
            conn.close()
        except Exception:
            pass              # already closed; reaping must never raise into a request
        _open.pop(ident, None)


def connect() -> sqlite3.Connection:
    """Open (and migrate) the database. Safe to call repeatedly, and FROM ANY THREAD.

    One connection per thread. A single shared one looks safe with
    `check_same_thread=False` and is not: reads here are an `execute` followed by a fetch
    and take no lock, while the write helpers hold `_lock` — so a write on another thread
    can reset a reader's statement between the two. The read then returns a row with empty
    columns rather than raising, and on THIS database an empty column is a credential
    decision:

        a session row read as None    -> 401, and the console bounces to /login
        account_id read as ''         -> 403 from the branch documented as unreachable

    Both are intermittent, neither reproduces under a single caller, and both look exactly
    like a revoked session to whoever is holding one. FastAPI runs every sync endpoint on a
    threadpool worker, so any polling page makes this continuous.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == _generation:
        return conn
    with _lock:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Autocommit. Every write here is a single statement except `verify`, which needs
        # an explicit BEGIN IMMEDIATE to make its read-modify-write atomic — and that
        # cannot be issued while Python's implicit transaction handling owns the
        # connection.
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")   # this is credentials, not telemetry
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(POST_MIGRATION)
        # Prune first, then register under this thread's ident. A recycled ident would
        # otherwise orphan the previous connection exactly as the old list did.
        _reap()
        ident = threading.get_ident()
        previous = _open.get(ident)
        if previous is not None:
            try:
                previous[1].close()
            except Exception:
                pass
        _open[ident] = (threading.current_thread(), conn)   # so `close()` reaches every
                                                            # thread's, and `_reap` the
                                                            # dead ones
        _local.conn = conn
        _local.generation = _generation
        return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing allowlist up to date. Idempotent.

    Automatic rather than a command to remember: the API and `admin_users.py` both open
    this file, and an allowlist where one process can see `account_id` and the other
    cannot is worse than a migration nobody asked for.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "account_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN account_id TEXT")

    # Backfill in a stable order so the ids an existing allowlist gets are reproducible
    # rather than dependent on however SQLite happened to return the rows.
    missing = [r[0] for r in conn.execute(
        "SELECT email FROM users WHERE account_id IS NULL OR account_id = '' "
        "ORDER BY created_at, email").fetchall()]
    for email in missing:
        conn.execute("UPDATE users SET account_id = ? WHERE email = ?",
                     (next_account_id(conn), email))


def use(path: Path | str) -> None:
    """Repoint at another database file. For tests, and for nothing else."""
    global _db_path
    close()
    with _lock:
        _db_path = Path(path)


def close() -> None:
    """Close every thread's connection, not just this one's.

    The generation bump is the load-bearing half. Closing another thread's handle without
    it leaves that thread holding a cached connection it will happily reuse, and sqlite
    answers `ProgrammingError: Cannot operate on a closed database` — from a worker that
    did nothing wrong. Bumping makes every cache miss, so the next call reopens.

    It is also what makes `use()` reach threads that already opened one: without it a
    worker keeps serving the previous allowlist.
    """
    global _generation
    with _lock:
        for _thread, conn in list(_open.values()):
            try:
                conn.close()
            except Exception:
                pass          # already closed, or its thread is gone; closing must not raise
        _open.clear()
        _generation += 1
    _local.conn = None


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def _row(sql: str, args: tuple = ()) -> dict | None:
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


# -------------------------------------------------------------------------------- users

def normalize_email(email: str) -> str:
    """The canonical form. Case-folded and trimmed, and nothing else.

    Gmail's dot- and plus-aliasing is deliberately NOT collapsed: `a.b@gmail.com` stays a
    different row from `ab@gmail.com`. Folding them would mean this service decides two
    addresses are one person based on a rule that is true at one provider and false at
    others, and it would silently widen an allowlist the owner wrote by hand.
    """
    return email.strip().lower()


def allow(email: str, label: str | None = None, is_admin: bool = False) -> dict:
    """Put an email on the allowlist, or reactivate and update one already there."""
    email = normalize_email(email)
    conn = connect()
    with _lock:
        # `account_id` is set on insert and DELIBERATELY absent from the update clause.
        # Re-allowing an address must never mint it a new id: the old one is stamped on
        # every strategy, fill and curve point that account owns in the desk's database,
        # and reissuing would orphan the lot while looking like a no-op here.
        conn.execute("""
            INSERT INTO users (email, account_id, label, is_admin, active, created_at)
            VALUES (?,?,?,?,1,?)
            ON CONFLICT(email) DO UPDATE SET
                label    = COALESCE(excluded.label, users.label),
                is_admin = excluded.is_admin,
                active   = 1
        """, (email, next_account_id(conn), label, int(is_admin), utcnow()))
    return user(email)                                              # type: ignore[return-value]


# ----------------------------------------------------------------------------- api keys

# The visible prefix. It is not a secret and it is not checked — its job is to make a
# leaked key recognisable in a log or a paste, so it can be found and revoked, and to let
# `current_principal` route a bearer credential without a database round trip.
KEY_PREFIX = "sk_live_"
KEY_BYTES = 32                      # 256 bits, which is what makes a fast hash safe here


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(email: str, label: str | None = None) -> tuple[str, dict]:
    """Mint a key for an allowlisted address. Returns `(raw_key, row)`.

    The raw key is returned ONCE and never stored — only its hash is. A key that can be
    read back out of the database is a key that a database leak hands to the reader, and
    "show it again" is a feature that quietly requires storing it in the clear.
    """
    email = normalize_email(email)
    if active_user(email) is None:
        raise ValueError(f"{email} is not on the allowlist")
    raw = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
    conn = connect()
    with _lock:
        cur = conn.execute("""
            INSERT INTO api_keys (key_hash, email, label, prefix, created_at)
            VALUES (?,?,?,?,?)
        """, (hash_key(raw), email, label, raw[:len(KEY_PREFIX) + 6], utcnow()))
        row = _row("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,))
    return raw, row                                       # type: ignore[return-value]


def api_key(key_hash: str, touch: bool = True) -> dict | None:
    """Resolve a key to its owner, or None.

    Joined against `users` on every call, exactly as `session` is: without it a revoked
    account keeps working until somebody remembers to revoke each of its keys separately,
    which is up to forever.
    """
    now = utcnow()
    row = _row("""SELECT k.id, k.email, k.label, k.prefix, k.created_at,
                         u.account_id, u.is_admin, u.label AS user_label
                  FROM api_keys k JOIN users u ON u.email = k.email
                  WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.active = 1""",
               (key_hash,))
    if row is None:
        return None
    if touch:
        with _lock:
            connect().execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                              (now, row["id"]))
    return row


def api_keys_for(email: str, live_only: bool = True) -> list[dict]:
    sql = ("SELECT id, label, prefix, created_at, last_used_at, revoked_at "
           "FROM api_keys WHERE email = ?")
    if live_only:
        sql += " AND revoked_at IS NULL"
    return _rows(sql + " ORDER BY id DESC", (normalize_email(email),))


def revoke_api_key(email: str, key_id: int) -> bool:
    """Scoped by email in the WHERE clause, so one account cannot revoke another's key
    by guessing an id."""
    conn = connect()
    with _lock:
        cur = conn.execute("""UPDATE api_keys SET revoked_at = ?
                              WHERE id = ? AND email = ? AND revoked_at IS NULL""",
                           (utcnow(), int(key_id), normalize_email(email)))
    return cur.rowcount > 0


def revoke_api_keys(email: str) -> int:
    conn = connect()
    with _lock:
        cur = conn.execute("""UPDATE api_keys SET revoked_at = ?
                              WHERE email = ? AND revoked_at IS NULL""",
                           (utcnow(), normalize_email(email)))
    return cur.rowcount


# --------------------------------------------------------------------- webhook secrets

# A different prefix from `sk_live_`, and that is load-bearing rather than cosmetic:
# `current_principal` routes a bearer credential BY PREFIX, so a webhook secret sent in an
# `Authorization` header falls through to the sessions table and answers 401. It opens the
# one route it was minted for and nothing else, which is the whole point of it existing.
WEBHOOK_PREFIX = "whk_"
WEBHOOK_BYTES = 32                  # 256 bits, same reasoning as an API key


def create_webhook_secret(account: str, strategy_id: str) -> tuple[str, dict]:
    """Mint (or rotate) the webhook secret for one strategy. Returns `(raw, row)`.

    Minting revokes whatever was live on that strategy, so there is never more than one
    working secret per alert and rotating is a single act. Returned once and stored as a
    hash, exactly as an API key is — a secret the database can hand back is a secret a
    database leak hands to the reader.
    """
    raw = WEBHOOK_PREFIX + secrets.token_hex(WEBHOOK_BYTES)
    now = utcnow()
    conn = connect()
    with _lock:
        conn.execute("""UPDATE webhook_secrets SET revoked_at = ?
                        WHERE strategy_id = ? AND account_id = ? AND revoked_at IS NULL""",
                     (now, strategy_id, account))
        cur = conn.execute("""
            INSERT INTO webhook_secrets
                (secret_hash, account_id, strategy_id, prefix, created_at)
            VALUES (?,?,?,?,?)
        """, (hash_key(raw), account, strategy_id,
              raw[:len(WEBHOOK_PREFIX) + 6], now))
        row = _row("SELECT * FROM webhook_secrets WHERE id = ?", (cur.lastrowid,))
    return raw, row                                       # type: ignore[return-value]


def webhook_secret(secret_hash: str, touch: bool = True) -> dict | None:
    """Resolve a webhook secret to its strategy and account, or None.

    The `EXISTS` clause is what `api_key`'s join against `users` is: a secret belonging to
    an account whose every sign-in address has been revoked stops working on the next call
    rather than whenever somebody remembers this table exists. `EXISTS` and not a join,
    because an account may have several addresses and a join would return the secret once
    per address.
    """
    row = _row("""SELECT * FROM webhook_secrets w
                  WHERE w.secret_hash = ? AND w.revoked_at IS NULL
                    AND EXISTS (SELECT 1 FROM users u
                                WHERE u.account_id = w.account_id AND u.active = 1)""",
               (secret_hash,))
    if row is None:
        return None
    if touch:
        with _lock:
            connect().execute("UPDATE webhook_secrets SET last_used_at = ? WHERE id = ?",
                              (utcnow(), row["id"]))
    return row


def webhook_secret_for(account: str, strategy_id: str) -> dict | None:
    """The live secret's METADATA for one strategy — never the secret itself."""
    return _row("""SELECT id, strategy_id, prefix, created_at, last_used_at
                   FROM webhook_secrets
                   WHERE account_id = ? AND strategy_id = ? AND revoked_at IS NULL
                   ORDER BY id DESC LIMIT 1""", (account, strategy_id))


def revoke_webhook_secret(account: str, strategy_id: str) -> int:
    """Turn the alert off. Scoped by account in the WHERE clause, so guessing a
    `strategy_id` cannot revoke somebody else's."""
    conn = connect()
    with _lock:
        cur = conn.execute("""UPDATE webhook_secrets SET revoked_at = ?
                              WHERE account_id = ? AND strategy_id = ?
                                AND revoked_at IS NULL""",
                           (utcnow(), account, strategy_id))
    return cur.rowcount


def account_id(email: str) -> str | None:
    """The id this address is known by outside the auth database."""
    row = user(email)
    return row.get("account_id") if row else None


def emails_for_account(account: str) -> list[str]:
    """Every address that signs in to one account. Usually one; more when linked."""
    return [r["email"] for r in
            _rows("SELECT email FROM users WHERE account_id = ? ORDER BY created_at, email",
                  (account,))]


def email_for_account(account: str) -> str | None:
    """The first address on an account. Only the API ever needs it — never the engine.

    "First" is by creation, so it is stable rather than whichever row SQLite returned. Use
    `emails_for_account` when the answer genuinely may be several.
    """
    emails = emails_for_account(account)
    return emails[0] if emails else None


def link_account(email: str, to_email: str) -> str:
    """Move `email` onto `to_email`'s account, so both sign in to one book.

    Returns the shared `account_id`. Deliberately explicit and deliberately named — it is
    the one operation that can merge two identities, and it must never be reachable by
    accident. Nothing in the web layer calls it; it is a shell command.

    The address keeps its own sessions, its own API keys and its own revocation. Only the
    account they resolve to is shared, which is exactly what "the same person with two
    mailboxes" means.
    """
    email, to_email = normalize_email(email), normalize_email(to_email)
    src, dst = user(email), user(to_email)
    if src is None:
        raise ValueError(f"{email} is not on the allowlist")
    if dst is None:
        raise ValueError(f"{to_email} is not on the allowlist")
    if not dst["account_id"]:
        raise ValueError(f"{to_email} has no account id")
    if email == to_email:
        raise ValueError("those are the same address")

    conn = connect()
    with _lock:
        conn.execute("UPDATE users SET account_id = ? WHERE email = ?",
                     (dst["account_id"], email))
    audit("account.linked", email, detail=f"now shares {dst['account_id']} with {to_email}")
    return dst["account_id"]


def revoke(email: str) -> bool:
    """Deactivate an account and kill its sessions.

    Both halves matter. Without the session sweep a revoked user keeps working until
    their token expires, which is up to thirty days of access granted by a command whose
    whole purpose was to take it away.
    """
    email = normalize_email(email)
    conn = connect()
    with _lock:
        cur = conn.execute("UPDATE users SET active = 0 WHERE email = ?", (email,))
        revoke_sessions(email)
        # Keys too. A revoked manager whose bot keeps its key would keep trading — and a
        # bot, unlike a browser, never closes its tab and notices. `api_key` also joins
        # against `users`, so this is belt and braces, deliberately: two independent
        # reasons a revoked account stops working.
        revoke_api_keys(email)
    return cur.rowcount > 0


def purge(email: str) -> bool:
    """Delete the account, its sessions and its challenges. The audit trail survives."""
    email = normalize_email(email)
    conn = connect()
    with _lock:
        cur = conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("DELETE FROM sessions WHERE email = ?", (email,))
        conn.execute("DELETE FROM otp_challenges WHERE email = ?", (email,))
    return cur.rowcount > 0


def user(email: str) -> dict | None:
    """The row whatever its state, so callers can tell 'revoked' from 'never existed'."""
    return _row("SELECT * FROM users WHERE email = ?", (normalize_email(email),))


def active_user(email: str) -> dict | None:
    return _row("SELECT * FROM users WHERE email = ? AND active = 1",
                (normalize_email(email),))


def users() -> list[dict]:
    return _rows("SELECT * FROM users ORDER BY active DESC, email")


def user_count() -> int:
    return int(connect().execute("SELECT COUNT(*) FROM users").fetchone()[0])


def mark_login(email: str) -> None:
    with _lock:
        connect().execute("UPDATE users SET last_login_at = ? WHERE email = ?",
                          (utcnow(), normalize_email(email)))


# ----------------------------------------------------------------------------- the code

def last_send_at(email: str) -> str | None:
    row = _row("""SELECT created_at FROM otp_challenges WHERE email = ?
                  ORDER BY id DESC LIMIT 1""", (normalize_email(email),))
    return row["created_at"] if row else None


def sends_since(email: str, since: str) -> int:
    return int(connect().execute(
        "SELECT COUNT(*) FROM otp_challenges WHERE email = ? AND created_at >= ?",
        (normalize_email(email), since)).fetchone()[0])


def ip_sends_since(ip: str, since: str) -> int:
    return int(connect().execute(
        "SELECT COUNT(*) FROM otp_challenges WHERE ip = ? AND created_at >= ?",
        (ip, since)).fetchone()[0])


def create_challenge(email: str, code_hash: str, ttl_seconds: int,
                     ip: str | None = None) -> dict:
    """Store a new code, retiring every live one for that address.

    Retiring the old ones is not tidiness. Leaving them live would let an attacker request
    twenty codes and then get twenty times `OTP_MAX_ATTEMPTS` guesses against the same
    million-wide space, which is the whole security margin of a six-digit code.
    """
    email = normalize_email(email)
    now = utcnow()
    conn = connect()
    with _lock:
        conn.execute("""UPDATE otp_challenges SET consumed_at = ?
                        WHERE email = ? AND consumed_at IS NULL""", (now, email))
        cur = conn.execute("""INSERT INTO otp_challenges
                              (email, code_hash, created_at, expires_at, ip)
                              VALUES (?,?,?,?,?)""",
                           (email, code_hash, now, _plus(ttl_seconds), ip))
        challenge_id = int(cur.lastrowid)
    return _row("SELECT * FROM otp_challenges WHERE id = ?", (challenge_id,))  # type: ignore[return-value]


def verify(email: str, code_hash: str, max_attempts: int) -> tuple[bool, str]:
    """Check a code against the live challenge, spending one attempt.

    Returns `(ok, reason)`. The reason is for the audit log and never for the response:
    telling a caller apart 'no code was requested' from 'wrong code' hands them a way to
    probe which addresses have live challenges.

    The whole read-modify-write runs inside one `BEGIN IMMEDIATE`, and the attempt is
    counted **before** the comparison. Counting after would mean a caller who cuts the
    connection mid-request gets unlimited free guesses.
    """
    email = normalize_email(email)
    now = utcnow()
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            row = conn.execute("""SELECT * FROM otp_challenges
                                  WHERE email = ? AND consumed_at IS NULL
                                  ORDER BY id DESC LIMIT 1""", (email,)).fetchone()
            result: tuple[bool, str]
            if row is None:
                result = (False, "no_challenge")
            elif row["expires_at"] <= now:
                result = (False, "expired")
            elif row["attempts"] >= max_attempts:
                result = (False, "locked")
            else:
                conn.execute(
                    "UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?",
                    (row["id"],))
                ok = hmac.compare_digest(str(row["code_hash"]), code_hash)
                if ok:
                    conn.execute("UPDATE otp_challenges SET consumed_at = ? WHERE id = ?",
                                 (now, row["id"]))
                result = (ok, "ok" if ok else "bad_code")
            conn.execute("COMMIT")
            committed = True
            return result
        finally:
            # A half-applied attempt counter is worse than none: it would let a caller who
            # can provoke an error here burn the challenge belonging to the real user.
            if not committed:
                conn.execute("ROLLBACK")


# ----------------------------------------------------------------------------- sessions

def create_session(email: str, token_hash: str, ttl_days: int,
                   ip: str | None = None, user_agent: str | None = None) -> dict:
    email = normalize_email(email)
    now = utcnow()
    conn = connect()
    with _lock:
        cur = conn.execute("""INSERT INTO sessions
                              (token_hash, email, created_at, expires_at, last_used_at,
                               ip, user_agent)
                              VALUES (?,?,?,?,?,?,?)""",
                           (token_hash, email, now, _plus(ttl_days * 86400), now,
                            ip, user_agent))
        session_id = int(cur.lastrowid)
    return _row("SELECT * FROM sessions WHERE id = ?", (session_id,))  # type: ignore[return-value]


def session(token_hash: str, touch: bool = True) -> dict | None:
    """Resolve a token to `{session, user}`, or None.

    The join against `users` is what makes `revoke` immediate for a *deactivated* account
    as well as a deleted one: an unexpired token belonging to an inactive user resolves to
    nothing here, so there is no path where a live token outlives the allowlist entry that
    justified it.
    """
    now = utcnow()
    # `account_id` comes back with the session because it is what every downstream reader
    # keys on — the board's per-account cut, the order ledger, the desk's record. Fetching
    # it separately would mean a second query on every authenticated request, and a path
    # where a session resolves but its account does not.
    row = _row("""SELECT s.*, u.label, u.is_admin, u.active, u.account_id
                  FROM sessions s JOIN users u ON u.email = s.email
                  WHERE s.token_hash = ? AND s.revoked_at IS NULL
                        AND s.expires_at > ? AND u.active = 1""", (token_hash, now))
    if row is None:
        return None
    if touch:
        with _lock:
            connect().execute("UPDATE sessions SET last_used_at = ? WHERE id = ?",
                              (now, row["id"]))
    return row


def revoke_session(token_hash: str) -> bool:
    conn = connect()
    with _lock:
        cur = conn.execute("""UPDATE sessions SET revoked_at = ?
                              WHERE token_hash = ? AND revoked_at IS NULL""",
                           (utcnow(), token_hash))
    return cur.rowcount > 0


def revoke_sessions(email: str) -> int:
    conn = connect()
    with _lock:
        cur = conn.execute("""UPDATE sessions SET revoked_at = ?
                              WHERE email = ? AND revoked_at IS NULL""",
                           (utcnow(), normalize_email(email)))
    return cur.rowcount


def sessions_for(email: str, live_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM sessions WHERE email = ?"
    args: tuple = (normalize_email(email),)
    if live_only:
        sql += " AND revoked_at IS NULL AND expires_at > ?"
        args += (utcnow(),)
    return _rows(sql + " ORDER BY created_at DESC", args)


# -------------------------------------------------------------------------------- audit

def audit(event: str, email: str | None = None, ip: str | None = None,
          detail: str | None = None) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO audit (ts, event, email, ip, detail) VALUES (?,?,?,?,?)",
            (utcnow(), event, normalize_email(email) if email else None, ip, detail))


def recent_audit(limit: int = 50, email: str | None = None) -> list[dict]:
    if email:
        return _rows("""SELECT * FROM audit WHERE email = ?
                        ORDER BY id DESC LIMIT ?""", (normalize_email(email), limit))
    return _rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------------- housekeeping

def purge_expired(keep_challenge_hours: int = 24) -> dict:
    """Drop what has no further use. Run at startup; cheap enough to ignore afterwards.

    Expired *sessions* go entirely — a dead token is not evidence of anything the audit
    table does not already hold. Challenges are kept for a day because they are the rate
    limiter's memory, and deleting them early would reset somebody's hourly cap.
    """
    conn = connect()
    now = utcnow()
    cutoff = ago(keep_challenge_hours * 3600)
    with _lock:
        s = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount
        c = conn.execute("DELETE FROM otp_challenges WHERE created_at <= ?",
                         (cutoff,)).rowcount
    return {"sessions": s, "challenges": c}


def summary() -> dict:
    conn = connect()
    q = lambda s, a=(): conn.execute(s, a).fetchone()[0]
    return {
        "db": str(_db_path),
        "users": q("SELECT COUNT(*) FROM users"),
        "active_users": q("SELECT COUNT(*) FROM users WHERE active = 1"),
        "live_sessions": q("""SELECT COUNT(*) FROM sessions
                              WHERE revoked_at IS NULL AND expires_at > ?""", (utcnow(),)),
        "challenges": q("SELECT COUNT(*) FROM otp_challenges"),
        "audit_events": q("SELECT COUNT(*) FROM audit"),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(summary(), indent=2))
