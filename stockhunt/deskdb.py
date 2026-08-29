"""The order ledger: what members asked the desk to do, and what it did about it.

This is the seam between `paper api/` and `paper trading engine/`. The API writes
requests; the desk reads them, acts, and writes outcomes back. Neither process calls the
other, neither imports the other, and if the web layer is down, wedged or compromised the
desk keeps trading whatever it already applied.

It lives in `stockhunt/` — the shared core — because both sides need the *same* schema and
two copies of a table definition is one definition and one liability. This module imports
nothing but the standard library, so pulling it into the API does not drag the trading
stack along behind it.

**One writer per column.** The API owns the request fields; the desk owns the outcome
fields (`state`, `filled_qty`, `avg_price`, `reason`, `applied_at`). Nothing writes both.
That is a weaker rule than the strict one-way file flow `live.json` uses, and it is the
right trade here: order state changes on every fill, and publishing it through a second
JSON document would mean a whole channel to keep in step for data that already has a
natural home.

**The desk trusts none of it.** Every field arriving from the API is re-validated against
the authoritative book before an order is submitted. The API's checks exist to give a
caller a fast, useful error; the desk's checks are the ones that bind.

Two properties this module is responsible for, and both are load-bearing:

* **Idempotency.** `client_order_id` is unique per account and required. Submitting the
  same one twice returns the first order rather than creating a second, so a network
  timeout, a retry loop or a bot restarting mid-flight cannot double a position.
* **Ordering.** `seq` is monotonic and the desk drains in `seq` order, so a cancel can
  never overtake the order it cancels.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stockhunt import paths

# Beside the desk's own record rather than in the API's `state/`, because the desk is the
# process that cannot tolerate losing it. Gitignored: `paper.db` is tracked because it is
# the forward-test record, but this holds members' order flow, which is their data and has
# no business in the repo's history.
DB_PATH = Path(os.environ.get("STOCKHUNT_DESK_DB")
               or (paths.PAPER / "state" / "desk.db"))

_lock = threading.RLock()

# ONE CONNECTION PER THREAD, and this is not an optimisation.
#
# A single shared `sqlite3.Connection` looks safe with `check_same_thread=False` and it is
# not. Reads here are `connect().execute(...)` followed by a fetch, and the write helpers
# hold `_lock` while reads do not — so a write on another thread can execute against the
# same connection between a reader's execute and its fetch, resetting the reader's
# statement. The read then returns a row whose columns are empty rather than raising, and
# an empty column is not obviously wrong to the code that receives it:
#
#     symbols ''      -> json.loads raises deep in `_shape`          -> HTTP 500
#     a session row   -> comes back as None                          -> HTTP 401
#     account_id ''   -> `api_auth` refuses a principal with no id   -> HTTP 403
#
# All three are intermittent, none reproduces under a single caller, and the 401 is the
# worst: the console reads it as "signed out" and bounces to a login the reader is already
# past. FastAPI runs every sync endpoint on a threadpool worker, so the moment anything
# polls, the race is continuous rather than theoretical.
#
# Per-thread connections remove the interleaving entirely rather than papering over it with
# a wider lock, and WAL is what makes that cheap — concurrent readers are the case it
# exists for, and the desk and the API were already two processes doing exactly this.
# `submit_order`'s BEGIN IMMEDIATE only becomes truthful here too: a transaction on a
# connection somebody else is issuing statements against was never really one.
_local = threading.local()
# Keyed by thread ident, and holding the Thread so liveness can be asked. It was a plain
# list, and that leaked a file descriptor per short-lived caller: `_local` drops its
# reference when a thread exits, but the list kept a strong one forever, so the connection
# was never collected and its fd never closed. `sqlite3.Connection` cannot be weak-
# referenced, so the registry has to be pruned rather than made weak — see `_reap`.
#
# It is the DESK that hits this. Nautilus fires `clock.set_timer` callbacks on a fresh
# thread each tick, so `desk_control` opens a new connection every second and the process
# reaches the 1024-fd soft limit in about eight minutes, after which every reconcile, drain
# and heartbeat fails with `unable to open database file` while systemd still reports the
# unit healthy. It does not reproduce on Windows, which has no comparable per-process fd
# ceiling — so this is a bug the dev box structurally cannot show you.
_open: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
# Bumped by `use()`. A thread caches its connection, so repointing the module has to
# invalidate every OTHER thread's cache as well — they compare this before reusing.
_generation = 0

# Terminal, in the sense that the desk will not touch the row again.
ORDER_DONE = ("filled", "canceled", "rejected", "expired")
REGISTRATION_DONE = ("retired", "rejected")

SCHEMA = """
-- A registration is a strategy the desk has been asked to run. Two kinds share one table
-- because they share a lifecycle and the desk applies them identically:
--
--   'member'      orders arrive over the API from somebody's own machine
--   'house_rule'  a talib rule promoted off a walk-forward sheet, traded to a target by
--                 TalibRuleStrategy. This is what "pick a strategy from the backtest and
--                 paper trade it" writes, and it belongs to account '00'.
--
-- `want` is the OWNER's intent and only the API writes it. `state` is what the desk has
-- actually done and only the desk writes it. They are separate because they genuinely
-- disagree for a while: pressing pause while the desk is down leaves want='paused' and
-- state='live' until the next tick, and that is the truth, not a bug to paper over.
CREATE TABLE IF NOT EXISTS registrations (
    strategy_id TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'member',
    cls         TEXT NOT NULL,
    symbols     TEXT NOT NULL,             -- JSON array; one venue's worth, see below
    tf          TEXT NOT NULL,
    -- The bars the strategy WATCHES, when that differs from the horizon it trades. NULL
    -- is the default and means they are the same. See `_add_late_columns`.
    signal_tf   TEXT,
    capital     REAL NOT NULL,
    -- How far the book may lever: gross exposure may not exceed `leverage` times equity.
    -- 1.0 is no leverage and is what every row written before this column existed means.
    -- See `_add_late_columns`.
    leverage    REAL NOT NULL DEFAULT 1.0,
    benchmark   TEXT,
    rule        TEXT,                      -- house_rule only
    allow_short INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    want        TEXT NOT NULL DEFAULT 'live',
    state       TEXT NOT NULL DEFAULT 'pending',
    reason      TEXT,
    applied_at  TEXT,
    UNIQUE(account, name)
);

-- A PORTFOLIO is a basket of strategies with ONE pot of money, one curve and one switch.
--
-- It owns no trading and holds no position. Its legs are ordinary rows in `registrations`
-- — `kind='book'`, exactly what a promotion writes — that additionally carry this table's
-- `portfolio_id`. That is the whole design, and it is deliberate: warm-up, fills, P&L,
-- curves and `desk_control`'s attach/retire already work for a book, so a parallel
-- registration type would be a second lifecycle to keep in step with the first, forever,
-- and the first one is the one that trades.
--
-- Two kinds, differing only in who chooses the legs:
--
--   'manual'  somebody picked the rules. Nothing re-checks them.
--   'follow'  it tracks the top `top_n` of ONE leaderboard sheet (`source_cls`,
--             `source_tf`), re-checked daily. The sheet moves and the basket moves with
--             it, which is why `portfolio_changes` exists.
--
-- `want` and `state` split for the same reason they do on a registration: the owner writes
-- the first, the desk writes the second, and they genuinely disagree while the desk
-- catches up. The toggle cascades `want` to every leg in ONE transaction — half a basket
-- switched off is a position nobody chose to hold.
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    account      TEXT NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'follow'
    -- The sheet a 'follow' portfolio tracks, and how deep into it. NULL on a 'manual' one:
    -- a source recorded for a basket nobody reconciles against it is a claim the row
    -- cannot keep.
    source_cls   TEXT,
    source_tf    TEXT,
    top_n        INTEGER,
    -- The pot, split equally across the live legs. The legs' own `capital` is derived from
    -- it and recomputed on every membership change, so this column is the one to edit.
    capital      REAL NOT NULL DEFAULT 100000.0,
    rebalance    TEXT NOT NULL DEFAULT 'monthly',
    want         TEXT NOT NULL DEFAULT 'live',
    state        TEXT NOT NULL DEFAULT 'pending',
    -- When the money started, which is not when the row was written: a portfolio built
    -- from a backtest can be dated to the day it was decided rather than the day it was
    -- typed in.
    inception    TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(account, name)
);

-- WHEN the basket changed and WHY. Append-only: nothing updates a row here and nothing
-- deletes one.
--
-- Same principle as `delete_registration`'s refusal, one level up. A 'follow' portfolio's
-- membership is decided by a sheet that moves underneath it, so without this its equity
-- curve has steps in it and nothing to explain them — and a composition that can be
-- rewritten afterwards is not a record of what was held.
CREATE TABLE IF NOT EXISTS portfolio_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    at           TEXT NOT NULL,
    action       TEXT NOT NULL,          -- 'added' | 'removed'
    strategy_id  TEXT,                   -- the leg, so the change joins to what traded
    cls          TEXT,
    tf           TEXT,
    rule         TEXT,
    -- Where the rule stood on the sheet when this happened, and which sheet that was.
    -- A 'follow' change reads as "it fell to 7th" only if the rank is written down at the
    -- time; the sheet is re-ranked nightly and cannot be asked afterwards.
    rank_at      INTEGER,
    source       TEXT,
    -- The basket AFTER the change: how many legs, and what each was resized to. Without
    -- them, reconstructing what a dollar was doing on a given day means replaying every
    -- row from inception and hoping none is missing.
    n_legs       INTEGER,
    leg_capital  REAL,
    reason       TEXT
);

-- `seq` is the drain order and the desk's clock. AUTOINCREMENT rather than plain rowid so
-- a deleted row can never have its number reused: the desk remembers how far it has read
-- as a watermark, and a reissued seq below that watermark would be skipped forever.
CREATE TABLE IF NOT EXISTS orders (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    action          TEXT NOT NULL DEFAULT 'new',      -- 'new' | 'cancel'
    -- Which order a cancel refers to, by the CALLER's id — the only one they know, since
    -- the venue's is minted inside the trading node and never leaves it. Its own
    -- `client_order_id` is separate and is what makes the cancel itself idempotent.
    target_coid     TEXT,
    symbol          TEXT,
    side            TEXT,
    qty             REAL,
    order_type      TEXT,
    limit_price     REAL,
    tif             TEXT,
    submitted_at    TEXT NOT NULL,
    -- Below here the DESK is the only writer.
    state           TEXT NOT NULL DEFAULT 'accepted',
    filled_qty      REAL NOT NULL DEFAULT 0,
    avg_price       REAL,
    reason          TEXT,
    applied_at      TEXT,
    UNIQUE(account, client_order_id)
);

-- How far the desk has drained. One row, so a restart resumes instead of replaying every
-- order ever sent — which for a 'new' action would place them all again.
CREATE TABLE IF NOT EXISTS watermark (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    seq     INTEGER NOT NULL DEFAULT 0,
    at      TEXT
);
INSERT OR IGNORE INTO watermark (id, seq) VALUES (1, 0);

-- The desk's pulse: one row, overwritten every pass. It answers the one question the rest
-- of this schema cannot, and the API needs it to tell the truth.
--
-- `want <> state` is what "the desk has not caught up yet" looks like, and it is IDENTICAL
-- whether the desk read the row a moment ago and is mid-pass, or has been down since
-- Tuesday. The console showed the same sentence for both — "the desk applies it on its
-- next pass" — which is a promise it had no way to keep. With a pulse, a request that is
-- merely in flight reads differently from one nobody is going to act on.
--
-- It is a heartbeat and NOT an acknowledgement: nothing waits on it, nothing retries on
-- it, and losing it costs a line of display and no correctness. The ledger stays the only
-- channel between the two processes.
CREATE TABLE IF NOT EXISTS heartbeat (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    at     TEXT,
    ticks  INTEGER NOT NULL DEFAULT 0,
    pid    INTEGER,
    node   TEXT,
    -- What went wrong on the last pass, or NULL. The desk catches per lane and carries on,
    -- which is right — one bad row must not stop a trading system — but it means a pass
    -- that fails EVERY time still completes, still beats, and still reports itself live
    -- while nothing it was asked to do ever happens. The pulse says the loop is running;
    -- this says whether the loop is getting anywhere.
    error  TEXT
);
INSERT OR IGNORE INTO heartbeat (id, ticks) VALUES (1, 0);

CREATE INDEX IF NOT EXISTS ix_orders_acct  ON orders(account, seq);
CREATE INDEX IF NOT EXISTS ix_orders_strat ON orders(strategy_id, seq);
CREATE INDEX IF NOT EXISTS ix_orders_open  ON orders(state, seq);
CREATE INDEX IF NOT EXISTS ix_reg_acct     ON registrations(account, state);
CREATE INDEX IF NOT EXISTS ix_pf_acct      ON portfolios(account, want);
CREATE INDEX IF NOT EXISTS ix_pf_changes   ON portfolio_changes(portfolio_id, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """Open the ledger. Safe to call repeatedly, from either process OR THREAD.

    Returns this thread's connection, opening one the first time. See the note above
    `_local` for why sharing a single one across threads corrupts reads rather than
    raising, which is the failure mode that costs the most to find.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == _generation:
        return conn
    with _lock:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None          # autocommit; see `submit_order` for the one
        conn.execute("PRAGMA journal_mode=WAL")     # place that needs its own transaction
        conn.execute("PRAGMA synchronous=NORMAL")
        # Two processes write this file and one of them is a live trading node. Without a
        # busy timeout, a write that collides with the other side's commit raises
        # `database is locked` — and it would do so inside a Nautilus event callback,
        # which is the worst place in the system to take an exception.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _add_late_columns(conn)
        _reap()                            # first, so a tick-per-thread caller stays flat
        ident = threading.get_ident()
        previous = _open.get(ident)
        if previous is not None:
            # Same ident, and this thread's cache was empty — so the entry belongs to a
            # thread that has exited and had its ident recycled, or to a generation `use()`
            # already closed. Either way it is nobody's live connection.
            try:
                previous[1].close()
            except Exception:
                pass
        _open[ident] = (threading.current_thread(), conn)   # so `close()` reaches every
        _local.conn = conn                                  # thread's, and `_reap` the
        _local.generation = _generation                     # dead ones
        return conn


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
            pass              # already closed; reaping must never raise into a callback
        _open.pop(ident, None)


def _add_late_columns(conn: sqlite3.Connection) -> None:
    """Columns that arrived after a database was first created.

    `CREATE TABLE IF NOT EXISTS` does nothing at all to a table that already exists, so a
    column added to SCHEMA later never reaches a file somebody is already running — and one
    of the two processes opening this file is a live trading node, which is not something to
    hand a fresh database to because a column moved.

    Deliberately additive and deliberately tiny. This is not a migration framework: a column
    with a default that older code ignores is the only schema change this seam should ever
    need, and anything bigger wants a versioned migration like `store._migrate`.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(heartbeat)")}
    if "error" not in have:
        conn.execute("ALTER TABLE heartbeat ADD COLUMN error TEXT")

    # How a book OBSERVES its bars, which is not always the horizon it trades. NULL means
    # "read bars of `tf` and act on the close", the only behaviour there has ever been.
    # Set to a finer timeframe, the desk reads those bars instead, folds each day into the
    # session so far and decides a few minutes before the bell — which is the only honest
    # way to trade a rule keyed on the current bar's own close.
    have = {r[1] for r in conn.execute("PRAGMA table_info(registrations)")}
    if "signal_tf" not in have:
        conn.execute("ALTER TABLE registrations ADD COLUMN signal_tf TEXT")

    # Which PORTFOLIO this book is a leg of, or NULL for the standalone kind — a rule
    # promoted by hand, or a member's own strategy. A leg is otherwise an ordinary
    # registration and every existing path treats it as one, which is the point.
    if "portfolio_id" not in have:
        conn.execute("ALTER TABLE registrations ADD COLUMN portfolio_id TEXT")

    # How far the book may lever. The DEFAULT is the whole reason this can be a late
    # column at all: `desk_orders.leverage_of` reads 1.0 for a row that has no value and
    # for a row that has 1.0, and 1.0 is bit-for-bit the cash rule the desk enforced before
    # leverage existed. So an existing registration keeps trading under exactly the terms
    # it was written under, with nothing to migrate and nothing to re-agree.
    #
    # `ALTER TABLE ... DEFAULT 1.0` backfills every existing row to 1.0 rather than to
    # NULL, which is what makes the two readings identical instead of merely equivalent.
    if "leverage" not in have:
        conn.execute("ALTER TABLE registrations ADD COLUMN leverage REAL NOT NULL "
                     "DEFAULT 1.0")
    # The index cannot live in SCHEMA with the others: `executescript` runs before this
    # function, so against a database created before the column existed it would be asked
    # to index a column that is not there yet, and the whole script would fail — taking
    # `connect()` down for a file the desk is mid-session on.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_reg_portfolio "
                 "ON registrations(portfolio_id)")


def use(path: Path | str) -> None:
    """Repoint at another file. For tests, and for nothing else."""
    global DB_PATH
    close()
    with _lock:
        DB_PATH = Path(path)


def close() -> None:
    """Close every thread's connection, not just this one's.

    The generation bump is the load-bearing half. Closing another thread's handle without
    it leaves that thread holding a cached connection it will happily reuse, and sqlite
    answers with `ProgrammingError: Cannot operate on a closed database` — from a worker
    that did nothing wrong, on a request that came later. Bumping makes every cache miss,
    so the next call on any thread reopens instead.
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


def _shape(row: dict | None) -> dict | None:
    """Decode the one column that is not a scalar."""
    if row is None:
        return None
    out = dict(row)
    if "symbols" in out and isinstance(out["symbols"], str):
        out["symbols"] = json.loads(out["symbols"])
    return out


# ============================================================ the API writes these

def register(account: str, name: str, cls: str, symbols: list[str], tf: str,
             capital: float, *, kind: str = "member", benchmark: str | None = None,
             rule: str | None = None, allow_short: bool = False,
             leverage: float = 1.0,
             signal_tf: str | None = None, portfolio_id: str | None = None) -> dict:
    """Ask the desk to run a strategy. Idempotent on `(account, name)`.

    Re-registering an existing name returns the existing row untouched rather than
    creating a second: a manager's deploy script that runs twice must not end up with two
    books under one name, quietly splitting their capital.

    `strategy_id` is derived from the account and name so it is stable and readable —
    `str_a7_meanrev` — rather than a random token nobody can correlate with anything.

    `portfolio_id` makes this row a LEG of a basket rather than a strategy standing on its
    own, and changes nothing else about it. Passing None leaves whatever the row already
    had, so reviving a leg does not orphan it from its portfolio.
    """
    conn = connect()
    existing = _shape(_row("SELECT * FROM registrations WHERE account = ? AND name = ?",
                           (account, name)))
    if existing is not None:
        # A retired or rejected registration is REVIVED rather than returned as it stands.
        #
        # Idempotence used to mean "return whatever is there", which read correctly and
        # behaved terribly: switching a book off and on again answered 201 Created with the
        # corpse, the desk saw nothing wanted, and the strategy stayed dead. Three attempts
        # in four seconds are in the audit log, all of them 201, none of them doing
        # anything.
        #
        # Registering means "this should be running". Reviving keeps the same
        # `strategy_id`, so the same `sid` and the same record continue — the downtime
        # becomes a measured gap rather than a new, shorter track record.
        if (existing["want"] == "retired"
                or existing["state"] in REGISTRATION_DONE):
            with _lock:
                conn.execute("""UPDATE registrations
                                SET want = 'live', state = 'pending', reason = NULL,
                                    applied_at = NULL,
                                    portfolio_id = COALESCE(?, portfolio_id)
                                WHERE strategy_id = ?""",
                             (portfolio_id, existing["strategy_id"]))
            return _shape(_row("SELECT * FROM registrations WHERE strategy_id = ?",
                               (existing["strategy_id"],)))
        return existing

    strategy_id = f"str_{account}_{name}"
    with _lock:
        conn.execute("""
            INSERT INTO registrations
                (strategy_id, account, name, kind, cls, symbols, tf, signal_tf, capital,
                 leverage, benchmark, rule, allow_short, created_at, want, state,
                 portfolio_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'live','pending',?)
        """, (strategy_id, account, name, kind, cls, json.dumps(list(symbols)), tf,
              signal_tf, float(capital), float(leverage), benchmark, rule,
              int(allow_short), utcnow(), portfolio_id))
    return _shape(_row("SELECT * FROM registrations WHERE strategy_id = ?",
                       (strategy_id,)))                      # type: ignore[return-value]


def set_want(account: str, strategy_id: str, want: str) -> bool:
    """The owner's intent: 'live', 'paused' or 'retired'.

    Scoped by account in the WHERE clause, not checked beforehand — a check-then-write
    leaves a window, and there is no reason for one account's statement to be able to name
    another's strategy at all.
    """
    if want not in ("live", "paused", "retired"):
        raise ValueError(f"unknown want: {want}")
    conn = connect()
    with _lock:
        cur = conn.execute(
            "UPDATE registrations SET want = ? WHERE strategy_id = ? AND account = ?",
            (want, strategy_id, account))
    return cur.rowcount > 0


def delete_registration(account: str, strategy_id: str) -> tuple[bool, str]:
    """Forget a registration that never traded. Returns `(deleted, why_not)`.

    **Deleting is otherwise refused, and that refusal is the point.** A forward test
    somebody can erase is not a record: a manager able to remove a losing run can remove
    the evidence of it, and a track record filtered by its own author is survivorship bias
    committed on purpose. `retire` therefore keeps everything, always.

    That principle protects EVIDENCE, and this is the one case with none. A registration
    that never received an order recorded nothing — no fill, no curve point, no row in the
    desk's `strategies` table — so removing it erases nothing and hides nothing. It is
    litter from a typo or a trial run, not history.

    Three conditions, and each is doing work:

    * **terminal.** A live or pending row is still the desk's to act on; deleting one out
      from under a running strategy is a different and much worse operation.
    * **`kind='member'`.** This is the load-bearing one. "No orders" proves "never traded"
      only for a strategy that trades ON INSTRUCTION. A house rule or a book trades itself
      — it fills constantly and submits no orders through this ledger at all, so the same
      test would read as "never traded" for something with months of record behind it.
    * **nothing ever FILLED.** Not "no orders" — that was the test until 2026-08-28 and it
      was the wrong one, by this docstring's own definition. The paragraph above says the
      removable case is "no fill, no curve point, no row in the desk's `strategies`
      table"; counting orders is a PROXY for that, and the proxy comes apart in exactly
      the case that matters. A registration the desk REFUSED never attached, so it has no
      instrument and no book and its orders cannot have filled — they sat in the ledger as
      a record of a client talking to something that never existed. That is litter, which
      is what this exception is for, and it was unremovable forever: retiring it did not
      help, because the orders stayed.

      So the test is `filled_qty > 0`. An unfilled order moved no money, opened no
      position and put no point on a curve; refusing to delete one protects nothing and
      strands the rows this function exists to clear. A single fill still locks the row
      permanently, which is the property that actually matters.
    """
    row = _shape(_row("SELECT * FROM registrations WHERE strategy_id = ? AND account = ?",
                      (strategy_id, account)))
    if row is None:
        return False, "no such strategy"
    if row["state"] not in REGISTRATION_DONE:
        return False, (f"it is {row['state']}. Retire it first — only a strategy the desk "
                       f"has finished with can be removed")
    if row["kind"] != "member":
        return False, (f"it is a {row['kind']}, which trades on its own rule rather than "
                       f"on orders. Its record is in the desk's book whether or not this "
                       f"ledger has anything for it")
    filled, submitted = connect().execute(
        "SELECT COUNT(*) FILTER (WHERE filled_qty > 0), COUNT(*) "
        "FROM orders WHERE strategy_id = ?", (strategy_id,)).fetchone()
    if filled:
        return False, (f"it has {filled} filled order{'s' if filled > 1 else ''}, so it "
                       f"has a record. A forward test somebody can erase is not a record")
    # The unfilled orders go WITH the registration, in one transaction. Deleting the row
    # and leaving them would put orders in the ledger pointing at a `strategy_id` that no
    # longer resolves — `strategy_of` would find nothing, and they would sit in `GET
    # /v1/orders` forever as the residue of a strategy the console says was removed. They
    # are the same litter by a different name.
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM orders WHERE strategy_id = ?", (strategy_id,))
            conn.execute("DELETE FROM registrations WHERE strategy_id = ? AND account = ?",
                         (strategy_id, account))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if submitted:
        return True, (f"removed, along with {submitted} order"
                      f"{'s' if submitted > 1 else ''} that never filled")
    return True, ""


def submit_order(account: str, strategy_id: str, client_order_id: str, *,
                 action: str = "new", symbol: str | None = None,
                 side: str | None = None, qty: float | None = None,
                 order_type: str = "market", limit_price: float | None = None,
                 tif: str = "day", target_coid: str | None = None) -> tuple[dict, bool]:
    """Append an order. Returns `(order, created)`.

    `created is False` means this `client_order_id` was already on file and the row
    returned is the original — the retry-safety the whole API rests on. The caller should
    answer 200 rather than 202 in that case, and must not treat it as an error: a client
    that retried after a timeout genuinely did want exactly this order, once.

    The insert is wrapped in `BEGIN IMMEDIATE` so the check and the write cannot be
    interleaved with another worker handling the same retry. Without it two concurrent
    retries both see "not there" and one dies on the UNIQUE constraint — correct, but as a
    500 rather than as the idempotent answer the caller was promised.
    """
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _row("SELECT * FROM orders WHERE account = ? AND client_order_id = ?",
                         (account, client_order_id))
            if prior is not None:
                conn.execute("COMMIT")
                return prior, False
            cur = conn.execute("""
                INSERT INTO orders (account, strategy_id, client_order_id, action,
                                    target_coid, symbol, side, qty, order_type,
                                    limit_price, tif, submitted_at, state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'accepted')
            """, (account, strategy_id, client_order_id, action, target_coid, symbol,
                  side, None if qty is None else float(qty), order_type,
                  None if limit_price is None else float(limit_price), tif, utcnow()))
            seq = int(cur.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _row("SELECT * FROM orders WHERE seq = ?", (seq,)), True   # type: ignore


# ============================================================ the API reads these

def registrations(account: str | None = None) -> list[dict]:
    sql = "SELECT * FROM registrations"
    args: tuple = ()
    if account is not None:
        sql += " WHERE account = ?"
        args = (account,)
    return [_shape(r) for r in _rows(sql + " ORDER BY created_at, strategy_id", args)]


def registration(strategy_id: str, account: str | None = None) -> dict | None:
    """One registration, optionally constrained to an account.

    Passing the account is how a member-facing read stays a member-facing read: without
    it, a caller who guesses a `strategy_id` reads somebody else's configuration.
    """
    sql = "SELECT * FROM registrations WHERE strategy_id = ?"
    args: tuple = (strategy_id,)
    if account is not None:
        sql += " AND account = ?"
        args = (strategy_id, account)
    return _shape(_row(sql, args))


def orders(account: str, *, strategy_id: str | None = None, state: str | None = None,
           since_seq: int = 0, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM orders WHERE account = ? AND seq > ?"
    args: list = [account, since_seq]
    if strategy_id:
        sql += " AND strategy_id = ?"
        args.append(strategy_id)
    if state:
        sql += " AND state = ?"
        args.append(state)
    return _rows(sql + " ORDER BY seq LIMIT ?", tuple(args + [limit]))


def order(account: str, client_order_id: str) -> dict | None:
    return _row("SELECT * FROM orders WHERE account = ? AND client_order_id = ?",
                (account, client_order_id))


def order_summary(account: str) -> dict[str, dict]:
    """Per strategy: how many orders, in what states, and the newest refusal in full.

    **This exists because the desk already explains every refusal and nothing rendered
    it.** `desk_orders` writes a precise, well-worded sentence into `orders.reason` —
    *"not enough cash: BTC/USD 2 at 77,640.45 costs 155,280.90 and this strategy holds
    10,000.00"* — and the only way to read it was to open this file over SSH. Meanwhile
    the strategy's page said "No fills yet", which is true and useless: a book that has had
    127 orders refused and a book nobody has ever sent an order to are completely different
    situations that printed the same sentence.

    **Two queries for the whole account, not one per strategy.** The console polls every
    two seconds and lists every registration, so a per-strategy call would be an N+1 on a
    timer. The counts group in SQLite and the refusal is one indexed scan.

    The reason is returned VERBATIM. It is the desk's sentence — the desk's checks are the
    ones that bind, and it is the only process that can see the book — so nothing between
    here and the screen re-derives or re-words it.
    """
    out: dict[str, dict] = {}
    for row in _rows("""SELECT strategy_id, state, COUNT(*) AS n
                        FROM orders WHERE account = ?
                        GROUP BY strategy_id, state""", (account,)):
        entry = out.setdefault(row["strategy_id"],
                               {"total": 0, "by_state": {}, "last_rejected": None})
        entry["by_state"][row["state"]] = int(row["n"])
        entry["total"] += int(row["n"])
    # The NEWEST refusal per strategy, which is the one worth showing: a bot in a loop
    # produces the same reason a hundred times, and the most recent is the state of the
    # world now rather than the first thing that ever went wrong.
    for row in _rows("""SELECT o.strategy_id, o.client_order_id, o.symbol, o.side, o.qty,
                               o.reason, o.submitted_at
                        FROM orders o
                        JOIN (SELECT strategy_id, MAX(seq) AS seq FROM orders
                              WHERE account = ? AND state = 'rejected'
                              GROUP BY strategy_id) newest
                          ON newest.strategy_id = o.strategy_id AND newest.seq = o.seq""",
                     (account,)):
        entry = out.setdefault(row["strategy_id"],
                               {"total": 0, "by_state": {}, "last_rejected": None})
        entry["last_rejected"] = dict(row)
    return out


def pulse(now: datetime | None = None) -> dict:
    """When the desk last finished a pass, and how long ago that was.

    `age_seconds` is None when the desk has never run against this ledger — a fresh file
    and a dead desk are different situations and the caller should be able to say which.

    Deliberately returns no verdict. Whether a given age counts as "down" is policy, it
    depends on the desk's configured tick, and it belongs to whoever is displaying it —
    this module knows the schema, not the tolerances.
    """
    row = _row("SELECT * FROM heartbeat WHERE id = 1") or {}
    at = row.get("at")
    age = None
    if at:
        try:
            age = ((now or datetime.now(timezone.utc))
                   - datetime.fromisoformat(at)).total_seconds()
        except ValueError:
            age = None
    return {"at": at, "ticks": int(row.get("ticks") or 0), "pid": row.get("pid"),
            "node": row.get("node"), "error": row.get("error"), "age_seconds": age}


def orders_since(account: str, since_iso: str) -> int:
    """How many orders this account has submitted since a timestamp.

    The rate limiter counts rows rather than keeping a window in memory, so a restart of
    the API does not hand a bot a fresh allowance — which is exactly when a bot in a retry
    loop would be hammering it.
    """
    return int(connect().execute(
        "SELECT COUNT(*) FROM orders WHERE account = ? AND submitted_at >= ?",
        (account, since_iso)).fetchone()[0])


# ============================================================ the desk writes these

def pending_registrations() -> list[dict]:
    """Everything the desk has not yet reconciled with what the owner asked for.

    Both directions: a `pending` row wants starting, and a live row whose `want` has moved
    away from its `state` wants pausing, resuming or retiring.
    """
    return [_shape(r) for r in _rows("""
        SELECT * FROM registrations
        WHERE state = 'pending'
           OR (state NOT IN ('retired', 'rejected') AND want <> state)
        ORDER BY created_at, strategy_id
    """)]


def active_registrations() -> list[dict]:
    """Everything that should exist on a desk right now — retired and rejected excluded.

    Distinct from `pending_registrations`, which answers "what has changed since the desk
    last looked". That question is the wrong one after a RESTART: a book the previous
    process started is `want=live, state=live`, so nothing has changed and the ledger is
    satisfied — while the new process holds no strategies at all. The desk came up empty
    and the record said everything was running.

    `state` is a claim about some process, not this one. What the desk actually holds is
    `_running`, so reconciliation compares against that and uses this as the target.
    """
    return [_shape(r) for r in _rows(f"""
        SELECT * FROM registrations
        WHERE state NOT IN ({','.join('?' * len(REGISTRATION_DONE))})
          AND want <> 'retired'
        ORDER BY created_at, strategy_id
    """, REGISTRATION_DONE)]


def unapplied_retirements() -> list[dict]:
    """Retired by the owner, not yet marked retired by the desk.

    These are invisible to `active_registrations`, which filters on `want <> 'retired'` —
    so a retire that lands while the desk is DOWN is seen by no reconciliation loop ever
    again, and the row keeps whatever `state` it had at the time. It reads `live` forever,
    for a strategy that no process is running and none ever will.

    Nothing is lost by it — the strategy is correctly not trading — but the record is
    wrong, and a record that says a dead book is live is the kind of wrong that gets
    believed.
    """
    return [_shape(r) for r in _rows(f"""
        SELECT * FROM registrations
        WHERE want = 'retired' AND state NOT IN ({','.join('?' * len(REGISTRATION_DONE))})
        ORDER BY created_at, strategy_id
    """, REGISTRATION_DONE)]


def mark_registration(strategy_id: str, state: str, reason: str | None = None) -> None:
    conn = connect()
    with _lock:
        conn.execute("""UPDATE registrations
                        SET state = ?, reason = ?, applied_at = ?
                        WHERE strategy_id = ?""",
                     (state, reason, utcnow(), strategy_id))


def beat(ticks: int, node: str | None = None, at: str | None = None,
         error: str | None = None) -> None:
    """Record that the desk finished a pass. Only the desk calls this.

    **A pulse means the loop is turning, not that the loop is working.** The desk guards
    each lane separately and carries on past a failure, which is correct — one malformed
    row must not stop a trading system — so a pass that fails identically every time still
    completes and still beats. `error` is what stops that reading as healthy: it carries
    the last pass's failure, and a beating pulse with an error on it is a desk that is up
    and getting nowhere. Passing None clears it, so a recovered pass looks recovered.

    `at` is injectable because the desk's clock is a Nautilus clock — under a TestClock it
    is not the wall clock, and a heartbeat that quietly used `datetime.now()` would report
    a backtest's desk as live today.
    """
    conn = connect()
    with _lock:
        conn.execute("""UPDATE heartbeat
                        SET at = ?, ticks = ?, pid = ?, node = ?, error = ?
                        WHERE id = 1""",
                     (at or utcnow(), int(ticks), os.getpid(), node, error))


def watermark_seq() -> int:
    return int(connect().execute("SELECT seq FROM watermark WHERE id = 1").fetchone()[0])


def drain(limit: int = 500) -> list[dict]:
    """Orders the desk has not seen yet, oldest first.

    Reading does NOT advance the watermark — `commit_drain` does, and only after the batch
    has actually been applied. A watermark advanced on read would lose every order in
    flight if the desk died mid-batch, and losing an order is the one failure a trading
    system may not have.
    """
    return _rows("SELECT * FROM orders WHERE seq > ? ORDER BY seq LIMIT ?",
                 (watermark_seq(), limit))


def commit_drain(seq: int) -> None:
    """Advance the watermark to `seq`. Monotonic: never moves backwards."""
    conn = connect()
    with _lock:
        conn.execute("UPDATE watermark SET seq = MAX(seq, ?), at = ? WHERE id = 1",
                     (int(seq), utcnow()))


def mark_order(seq: int, state: str, *, filled_qty: float | None = None,
               avg_price: float | None = None, reason: str | None = None) -> None:
    """Record what happened to one order. Only the desk calls this.

    A row already in a terminal state is left alone. Nautilus can deliver a late event for
    an order that was rejected or canceled minutes earlier, and letting it reopen a closed
    order would make the ledger disagree with the book.
    """
    conn = connect()
    with _lock:
        conn.execute(f"""
            UPDATE orders SET
                state      = ?,
                filled_qty = COALESCE(?, filled_qty),
                avg_price  = COALESCE(?, avg_price),
                reason     = COALESCE(?, reason),
                applied_at = ?
            WHERE seq = ? AND state NOT IN ({','.join('?' * len(ORDER_DONE))})
        """, (state, filled_qty, avg_price, reason, utcnow(), int(seq), *ORDER_DONE))


def stale_orders(max_age: timedelta, now: datetime | None = None) -> list[dict]:
    """Undrained orders older than `max_age`.

    An order held while the desk was down and then filled hours later at a price the
    manager never saw is worse than one rejected outright — they can retry a rejection,
    and they cannot undo a fill. The desk rejects these instead of executing them; the
    window is a policy decision and lives in `desk_orders`, not here.
    """
    cutoff = ((now or datetime.now(timezone.utc)) - max_age).isoformat(timespec="seconds")
    return _rows("""SELECT * FROM orders
                    WHERE seq > ? AND submitted_at < ? ORDER BY seq""",
                 (watermark_seq(), cutoff))
