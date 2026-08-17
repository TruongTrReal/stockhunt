"""Durable storage for the paper desk: `results/paper.db`.

Before this, `paper_state.json` was the only record and it was write-only — nothing ever
read it back, so every `run_paper.py` start began with an empty registry and the forward
record restarted at zero. A forward test that resets on restart is not a forward test; it
is a series of unrelated day-one snapshots.

**Events, not state.** The tables hold what happened — fills and curve points — and the
JSON document the dashboard reads is a *projection* rendered from them. A state blob
cannot be merged across restarts without guessing; an append-only event log can, and it
also makes any later question answerable ("what did this rule do in March") without having
kept a special-purpose file for it.

**Idempotent by construction.** On restart Nautilus replays warm-up bars and the strategy
may re-emit fills it already reported. Every insert is `INSERT OR IGNORE` against a
natural-key UNIQUE index, so a replayed event is silently dropped rather than double
counted. This is the property the whole design rests on: correctness does not depend on
the caller remembering what it already sent.

**Gaps are recorded, never smoothed over.** While the desk is stopped the strategy holds
nothing, so its return over the gap is genuinely 0 — but the benchmark keeps compounding.
Chaining both at 0 would flatter the strategy in a falling market, so the benchmark's
actual return across each gap is fetched from price data and stored. When it cannot be
determined the gap is stored with `bench_pct = NULL` and stays visibly unknown rather than
quietly becoming zero.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import threading
from datetime import datetime, timezone

import fill_pnl
import paper_config

DB_PATH = paper_config.RESULTS_DIR / "paper.db"

# Points kept in the rendered curve. The database keeps everything; this is only how much
# of it the browser is asked to draw.
MAX_CURVE_POINTS = 400
MAX_TRADES = 200

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_session_id: int | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    pid         INTEGER
);

-- `account` is who owns this system's book: '00' is the house — the rules this desk runs
-- off its own walk-forward sheets — and every other value is a member. It is a two-character
-- id and never an email, so nothing in the trading engine holds personal data.
--
-- `kind` separates 'house_rule' (a talib rule traded to a target by TalibRuleStrategy)
-- from 'member' (orders arriving over the API). They keep the same record shape on
-- purpose: one curve, one fill log, one set of gaps, so the dashboard and every reader
-- downstream cannot tell them apart and does not have to.
--
-- `benchmark` is DECLARED, never inferred. A house rule benchmarks against buy-and-hold of
-- its own symbol; a member strategy may hold several names and has no obvious baseline, so
-- picking one would be this desk's opinion appearing inside somebody else's track record.
-- NULL means "no benchmark", which `lifetime_curve` already handles as unknown.
CREATE TABLE IF NOT EXISTS strategies (
    sid         TEXT PRIMARY KEY,
    account     TEXT NOT NULL DEFAULT '00',
    kind        TEXT NOT NULL DEFAULT 'house_rule',
    symbol      TEXT, cls TEXT, tf TEXT, rule TEXT, venue TEXT,
    benchmark   TEXT,
    capital     REAL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    bt_ir       REAL, bt_years REAL, note TEXT
);

-- The natural key is the fill itself. Two genuinely distinct fills for one strategy at the
-- same bar time, same size and same price are indistinguishable and would not
-- be separable by any key that does not come from the venue -- and on a replay the venue
-- ids are regenerated, so they are no help. Collapsing them is the safe direction: a
-- double-counted fill corrupts the record, a dropped duplicate does not.
--
-- `symbol` is IN the key. Without it the key is unambiguous only while a strategy holds
-- exactly one instrument, which is true of every house rule and false of the first member
-- strategy that trades two names: buying 10 of each at the same price on the same bar
-- would collapse to one row and lose half the position. It carries a NOT NULL default
-- rather than being nullable because SQLite treats NULLs as distinct in a UNIQUE index,
-- which would silently switch the deduplication off for any row that omitted it.
--
-- `ref` is how the two kinds of strategy get the deduplication each of them needs, and
-- they need OPPOSITE things:
--
--   house rules  leave it ''. A restart replays warm-up bars and the strategy re-emits
--                fills it already reported, so identical fills MUST collapse. That is the
--                property `test_store.py` protects and it stays exactly as it was.
--   members      pass the venue's own trade id. A manager can legitimately send the same
--                order twice on one bar — same symbol, side, size and price — and those
--                are two real fills. Collapsing them would lose half the position. Their
--                orders are never replayed, because the desk only drains past its
--                watermark, so a unique id per fill is safe here and not there.
-- `book_pnl` and `realised_pnl` are two different questions and the desk answers both,
-- because it used to answer the first one under the second one's name:
--
--   book_pnl      equity - capital at the moment of this fill. A snapshot of the WHOLE
--                 book, so several names filling in one second all carry the same value.
--   realised_pnl  what THIS fill closed, against the position's average cost. NULL on a
--                 fill that opened or added — such a fill realises nothing, which is not
--                 the same fact as realising zero, and a NULL is what keeps an opening
--                 buy out of the closed-trade statistics. `fill_pnl` owns the arithmetic.
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sid          TEXT NOT NULL,
    session_id   INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL DEFAULT '',
    side         TEXT NOT NULL,
    qty          REAL NOT NULL,
    price        REAL NOT NULL,
    book_pnl     REAL,
    realised_pnl REAL,
    ref          TEXT NOT NULL DEFAULT '',
    UNIQUE(sid, ts, symbol, side, qty, price, ref)
);

-- equity_pct and bench_pct are percent from the START OF THEIR OWN SESSION, not from
-- inception. Chaining across sessions happens at read time in `lifetime_curve`, because
-- that is where the gap returns are known.
CREATE TABLE IF NOT EXISTS curve (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT NOT NULL,
    session_id  INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    equity_pct  REAL NOT NULL,
    bench_pct   REAL NOT NULL,
    UNIQUE(sid, ts)
);

CREATE TABLE IF NOT EXISTS gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT NOT NULL,
    from_ts     TEXT NOT NULL,
    to_ts       TEXT NOT NULL,
    bench_pct   REAL,
    UNIQUE(sid, from_ts, to_ts)
);

CREATE INDEX IF NOT EXISTS ix_fills_sid ON fills(sid, ts);
CREATE INDEX IF NOT EXISTS ix_curve_sid ON curve(sid, ts);
CREATE INDEX IF NOT EXISTS ix_gaps_sid  ON gaps(sid, from_ts);
"""

# Indexes over columns that `_migrate` adds, so they CANNOT live in SCHEMA above.
# `CREATE TABLE IF NOT EXISTS` is a no-op against a legacy table, which then still lacks
# `account` — and the index statement fails the whole script before the migration that
# would have added the column ever runs. Ordering, not preference.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_strat_acct ON strategies(account, kind);
"""

# The house. Reserved, and never handed to a member — `authdb.allow` starts at '01'.
HOUSE = "00"

# Bumped whenever `SCHEMA` changes shape. `PRAGMA user_version` is the marker rather than
# inspecting the data, because "has this run already?" must be answerable on an empty
# database too, where every data-shaped test looks like "not yet".
SCHEMA_VERSION = 3


def sid_for(account: str, name: str) -> str:
    """The record's identity for one system: `00:spy-1d-sma_200`.

    The account goes IN FRONT, and that placement is not cosmetic. Nautilus caps
    `order_id_tag` at 36 characters and `run_paper` already slices it, so an account
    written as a suffix is the part that gets truncated away — two members on the same
    long-named rule would collapse into one tag and Nautilus would reject the second
    registration. In front, the discriminator is the one thing that always survives.

    Two characters, so the prefix costs three of the 36 and the existing tags still fit.
    """
    return f"{account}:{name}"


def _migrate(conn: sqlite3.Connection) -> bool:
    """Bring an existing database up to `SCHEMA_VERSION`. Idempotent; returns True if it
    did anything.

    Run automatically from `connect()` rather than left to a command somebody has to
    remember, because several processes open this file — the desk, the dashboard builder,
    the tests — and a half-migrated set of readers is a worse failure than a migration
    nobody asked for. `migrate_owner.py --check` is how a human inspects it.

    `results/paper.db` is tracked in git precisely because it is the forward record, which
    means this is revertible with `git checkout` if it goes wrong. That is the safety net;
    do not remove the tracking without replacing it.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return False

    had_rows = int(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0])

    # --- v0 -> v1 -------------------------------------------------------------------
    scols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)")}
    if "account" not in scols:
        conn.execute("ALTER TABLE strategies ADD COLUMN account TEXT NOT NULL DEFAULT '00'")
    if "kind" not in scols:
        conn.execute("ALTER TABLE strategies ADD COLUMN kind TEXT NOT NULL "
                     "DEFAULT 'house_rule'")
    if "benchmark" not in scols:
        conn.execute("ALTER TABLE strategies ADD COLUMN benchmark TEXT")

    # `fills` needs a widened UNIQUE, and SQLite cannot alter a constraint in place — so
    # the table is rebuilt. The symbol is backfilled from `strategies` rather than left
    # empty: every legacy sid names exactly one instrument, so the value is recoverable
    # and exact. Leaving it blank would work until the desk restarted and re-emitted a
    # warm-up fill WITH a symbol, which would then miss the old row and double-count.
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    if "symbol" not in fcols or "ref" not in fcols:
        conn.executescript("""
            CREATE TABLE fills_v2 (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sid         TEXT NOT NULL,
                session_id  INTEGER NOT NULL,
                ts          TEXT NOT NULL,
                symbol      TEXT NOT NULL DEFAULT '',
                side        TEXT NOT NULL,
                qty         REAL NOT NULL,
                price       REAL NOT NULL,
                book_pnl    REAL,
                ref         TEXT NOT NULL DEFAULT '',
                UNIQUE(sid, ts, symbol, side, qty, price, ref)
            );
        """)
        # Built by name rather than by position: v0 has no `symbol` and v1 has no `ref`,
        # and `SELECT *` across a schema change is how a migration silently transposes two
        # columns. The symbol is backfilled from `strategies` — every legacy sid names one
        # instrument, so it is recoverable and exact, and leaving it blank would let a
        # restart's replayed fill miss the old row and double-count.
        has_symbol = "symbol" in fcols
        conn.execute(f"""
            INSERT OR IGNORE INTO fills_v2
                (id, sid, session_id, ts, symbol, side, qty, price, book_pnl, ref)
            SELECT f.id, f.sid, f.session_id, f.ts,
                   {'f.symbol' if has_symbol else
                    "COALESCE((SELECT s.symbol FROM strategies s WHERE s.sid = f.sid), '')"},
                   f.side, f.qty, f.price, f.book_pnl, ''
            FROM fills f
        """)
        conn.executescript("""
            DROP TABLE fills;
            ALTER TABLE fills_v2 RENAME TO fills;
            CREATE INDEX IF NOT EXISTS ix_fills_sid ON fills(sid, ts);
        """)

    # Every existing system belongs to the house. The prefix is applied to all four tables
    # in one transaction: a sid is a foreign key in everything but name, and prefixing
    # `strategies` alone would orphan every fill, curve point and gap ever recorded.
    #
    # Guarded on `version < 1` and NOT on the target version, because this step is not
    # idempotent — running it again on an already-prefixed database produces `00:00:spy-…`
    # and orphans everything a second time. Every future schema bump would trip it. The
    # column additions above are safe to re-run because they test for their own column;
    # this one has nothing to test for, so the version has to carry it.
    if version < 1:
        for table in ("strategies", "fills", "curve", "gaps"):
            conn.execute(f"UPDATE {table} SET sid = ? || sid", (HOUSE + ":",))
        conn.execute("UPDATE strategies SET account = ?, kind = 'house_rule' "
                     "WHERE account IS NULL OR account = ''", (HOUSE,))

    # --- v2 -> v3 -------------------------------------------------------------------
    # `realised_pnl`: what each fill actually closed. The column the board was reading
    # before this is `book_pnl`, which is the whole book's mark at the fill and not a
    # trade result at all — see the `fills` table comment for what that cost.
    #
    # Backfilled rather than left NULL, because the fills ARE the input: replaying one
    # symbol's own fills in order recovers the value exactly, so a record written before
    # the column existed is repaired instead of starting blank. Guarded on the column
    # being absent rather than on the version, so it can never overwrite a value the live
    # desk has since written.
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    if "realised_pnl" not in fcols:
        conn.execute("ALTER TABLE fills ADD COLUMN realised_pnl REAL")
        rows = conn.execute("""SELECT id, sid, symbol, side, qty, price FROM fills
                               ORDER BY sid, symbol, ts, id""").fetchall()
        done = 0
        for _, group in itertools.groupby(rows, key=lambda r: (r[1], r[2])):
            group = list(group)
            for row, realised in zip(group, fill_pnl.replay(
                    [(r[3], r[4], r[5]) for r in group])):
                if realised is not None:
                    conn.execute("UPDATE fills SET realised_pnl = ? WHERE id = ?",
                                 (round(realised, 6), row[0]))
                    done += 1
        if rows:
            print(f"  store: recovered realised P&L for {done} of {len(rows)} fills "
                  f"(the rest opened or added to a position and closed nothing)")

    conn.executescript(POST_MIGRATION_INDEXES)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    if had_rows:
        print(f"  store: migrated {had_rows} strategies to schema v{SCHEMA_VERSION} "
              f"(account prefix '{HOUSE}:', symbol in the fill key)")
    return True


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """Open (and migrate) the database. Safe to call repeatedly."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        # WAL so the dashboard can read while the desk writes. Without it a reader can
        # block a fill from being recorded, which is exactly backwards.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        # After the schema, not before: `_migrate` reads `PRAGMA table_info` and needs the
        # tables to exist. On a fresh database the script above already built the target
        # shape, so migration only stamps the version and touches nothing.
        _migrate(conn)
        _conn = conn
        return conn


def start_session() -> int:
    """Open a session row and return its id. One per `run_paper.py` process."""
    global _session_id
    conn = connect()
    with _lock:
        cur = conn.execute("INSERT INTO sessions (started_at, pid) VALUES (?, ?)",
                           (_utc(), os.getpid()))
        conn.commit()
        _session_id = int(cur.lastrowid)
        return _session_id


def end_session() -> None:
    if _session_id is None:
        return
    conn = connect()
    with _lock:
        conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (_utc(), _session_id))
        conn.commit()


def session_id() -> int:
    return _session_id if _session_id is not None else start_session()


def upsert_strategy(sid: str, **f) -> str:
    """Register a strategy, preserving `first_seen` across restarts.

    Returns the stored `first_seen`, which is what the desk shows as "since" — the date
    this system started trading, not the date this process started.
    """
    conn = connect()
    now = _utc()
    # The account is taken from the sid rather than trusted as a separate argument: they
    # are two spellings of one fact, and a row whose `account` column disagreed with its
    # own key is a row that would be invisible to one query and visible to another.
    account = sid.split(":", 1)[0] if ":" in sid else HOUSE
    with _lock:
        conn.execute("""
            INSERT INTO strategies (sid, account, kind, symbol, cls, tf, rule, venue,
                                    benchmark, capital, first_seen, last_seen,
                                    bt_ir, bt_years, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sid) DO UPDATE SET
                account=excluded.account, kind=excluded.kind,
                symbol=excluded.symbol, cls=excluded.cls, tf=excluded.tf,
                rule=excluded.rule, venue=excluded.venue,
                benchmark=excluded.benchmark, capital=excluded.capital,
                last_seen=excluded.last_seen, bt_ir=excluded.bt_ir,
                bt_years=excluded.bt_years, note=excluded.note
        """, (sid, account, f.get("kind", "house_rule"), f.get("symbol"), f.get("cls"),
              f.get("tf"), f.get("rule"), f.get("venue"), f.get("benchmark"),
              f.get("capital"), now, now, f.get("bt_ir"),
              f.get("bt_years"), f.get("note")))
        conn.commit()
        row = conn.execute("SELECT first_seen FROM strategies WHERE sid = ?",
                           (sid,)).fetchone()
    return row[0] if row else now


def record_fill(sid: str, ts: str, side: str, qty: float, price: float,
                book_pnl: float = 0.0, symbol: str = "", ref: str = "",
                realised_pnl: float | None = None) -> None:
    """One fill. `symbol` and `ref` are both part of the natural key.

    Leave `ref` empty for a rule-driven strategy, where identical fills on one bar are a
    warm-up replay and MUST collapse. Pass the venue's trade id for an order-driven one,
    where they are two real fills a manager asked for. The `fills` table comment has the
    full reasoning.

    `realised_pnl` stays None on a fill that opened or added to a position — it closed
    nothing, which the statistics downstream must be able to tell apart from closing at
    zero. It is deliberately NOT part of the natural key: it is a consequence of the fill,
    not part of its identity, and a replayed fill computed against a re-warmed book could
    carry a different one and stop collapsing.
    """
    conn = connect()
    sess = session_id()          # resolved BEFORE the lock: it may open a session, which
    with _lock:                  # takes the same non-reentrant lock and would deadlock
        conn.execute("""INSERT OR IGNORE INTO fills
                        (sid, session_id, ts, symbol, side, qty, price, book_pnl,
                         realised_pnl, ref)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (sid, sess, ts, symbol or "", side, float(qty), float(price),
                      float(book_pnl),
                      None if realised_pnl is None else float(realised_pnl),
                      ref or ""))
        conn.commit()


def record_point(sid: str, equity_pct: float, bench_pct: float,
                 ts: str | None = None) -> None:
    conn = connect()
    sess = session_id()          # see record_fill: resolve outside the lock
    with _lock:
        conn.execute("""INSERT OR IGNORE INTO curve
                        (sid, session_id, ts, equity_pct, bench_pct) VALUES (?,?,?,?,?)""",
                     (sid, sess, ts or _utc(), float(equity_pct), float(bench_pct)))
        conn.commit()


def record_gap(sid: str, from_ts: str, to_ts: str, bench_pct: float | None) -> None:
    conn = connect()
    with _lock:
        conn.execute("""INSERT OR IGNORE INTO gaps (sid, from_ts, to_ts, bench_pct)
                        VALUES (?,?,?,?)""", (sid, from_ts, to_ts, bench_pct))
        conn.commit()


# ------------------------------------------------------------------------------ reading

def last_point(sid: str) -> tuple[str, float, float] | None:
    """(ts, equity_pct, bench_pct) of the most recent point, across all sessions."""
    conn = connect()
    row = conn.execute("""SELECT ts, equity_pct, bench_pct FROM curve
                          WHERE sid = ? ORDER BY ts DESC LIMIT 1""", (sid,)).fetchone()
    return (row[0], row[1], row[2]) if row else None


def fill_count(sid: str) -> int:
    conn = connect()
    return int(conn.execute("SELECT COUNT(*) FROM fills WHERE sid = ?",
                            (sid,)).fetchone()[0])


def recent_fills(sid: str, limit: int = MAX_TRADES) -> list[dict]:
    """The published fills, oldest first, with BOTH P&L columns.

    `pnl` is the book snapshot and keeps its name for the dashboards already reading it;
    `realised` is what the fill closed and is **None on a fill that closed nothing** — the
    board counts closed trades off that null, never off `pnl != 0`, which is what used to
    make an opening buy a closed trade.
    """
    conn = connect()
    rows = conn.execute("""SELECT ts, side, qty, price, book_pnl, symbol, realised_pnl
                           FROM fills
                           WHERE sid = ? ORDER BY ts DESC, id DESC LIMIT ?""",
                        (sid, limit)).fetchall()
    return [{"ts": r[0], "side": r[1], "qty": r[2], "price": r[3],
             "pnl": round(r[4] or 0.0, 2), "symbol": r[5] or "",
             "realised": None if r[6] is None else round(r[6], 2)}
            for r in reversed(rows)]


def strategies_for(account: str | None = None) -> list[dict]:
    """Registered systems, optionally just one account's.

    `None` means every account and is what the desk itself uses at start-up; a member view
    always passes one. Filtering here rather than at the caller means there is a single
    place where "whose is this" is decided.
    """
    conn = connect()
    sql = ("SELECT sid, account, kind, symbol, cls, tf, rule, venue, benchmark, "
           "capital, first_seen, last_seen, note FROM strategies")
    args: tuple = ()
    if account is not None:
        sql += " WHERE account = ?"
        args = (account,)
    sql += " ORDER BY account, sid"
    cols = ("sid", "account", "kind", "symbol", "cls", "tf", "rule", "venue",
            "benchmark", "capital", "first_seen", "last_seen", "note")
    return [dict(zip(cols, row)) for row in conn.execute(sql, args).fetchall()]


def lifetime_curve(sid: str) -> dict:
    """Chain every session's curve into one series from inception.

    Sessions store percent from their own start, so they are converted to multiples and
    multiplied together. Between two sessions the strategy held nothing, so its multiple
    for the gap is exactly 1.0; the benchmark's is whatever the market did, taken from the
    `gaps` table. A gap whose benchmark return is unknown contributes 1.0 as well and is
    reported in `unknown_gaps`, so the reader knows the benchmark line is understated
    rather than being told a zero that was never measured.
    """
    conn = connect()
    # Ordered by session first. Sessions are monotonic in time, but a warm-up replay can
    # insert bars that predate the current session's own first bar, and ordering by ts
    # alone would then interleave two sessions and invent a break at every crossing.
    rows = conn.execute("""SELECT session_id, ts, equity_pct, bench_pct FROM curve
                           WHERE sid = ? ORDER BY session_id, ts""", (sid,)).fetchall()
    if not rows:
        return {"equity": [], "bench": [], "breaks": [], "gaps": 0, "unknown_gaps": 0}

    # Several restarts can happen before the next bar closes, each recording a gap from the
    # same last point to a later "now". They describe one continuous stretch of downtime,
    # so the chain must use the widest of them — hence ordering by to_ts and letting the
    # last write win, rather than depending on row order for a tie.
    gap_rows = conn.execute("""SELECT from_ts, to_ts, bench_pct FROM gaps
                               WHERE sid = ? ORDER BY from_ts, to_ts""", (sid,)).fetchall()
    return _rechain(rows, {g[0]: g for g in gap_rows})


def _rechain(rows, gaps) -> dict:
    """The chaining, done in one pass per session rather than per point.

    Kept separate from `lifetime_curve` because the per-point form is easy to get subtly
    wrong: the stored percent is relative to its own session, so the carry must advance
    exactly once per session boundary, using that session's FINAL value.
    """
    eq_out: list[float] = []
    bn_out: list[float] = []
    breaks: list[int] = []
    eq_carry, bn_carry = 1.0, 1.0
    unknown = 0

    # group by session, preserving order
    groups: list[tuple[int, list]] = []
    for session, ts, eq_pct, bn_pct in rows:
        if not groups or groups[-1][0] != session:
            groups.append((session, []))
        groups[-1][1].append((ts, eq_pct, bn_pct))

    for i, (_session, pts) in enumerate(groups):
        if i > 0:
            breaks.append(len(eq_out))
            prev_last_ts = groups[i - 1][1][-1][0]
            g = gaps.get(prev_last_ts)
            if g is not None and g[2] is not None:
                bn_carry *= 1.0 + float(g[2]) / 100.0
            else:
                unknown += 1
            # the strategy held nothing across the gap: its multiple is exactly 1.0
        for ts, eq_pct, bn_pct in pts:
            eq_out.append(round((eq_carry * (1.0 + eq_pct / 100.0) - 1.0) * 100.0, 4))
            bn_out.append(round((bn_carry * (1.0 + bn_pct / 100.0) - 1.0) * 100.0, 4))
        last_eq, last_bn = pts[-1][1], pts[-1][2]
        eq_carry *= 1.0 + last_eq / 100.0
        bn_carry *= 1.0 + last_bn / 100.0

    eq_out, bn_out, breaks = _thin(eq_out, bn_out, breaks)
    return {"equity": eq_out, "bench": bn_out, "breaks": breaks,
            "gaps": max(len(groups) - 1, 0), "unknown_gaps": unknown}


def _thin(eq: list[float], bn: list[float], breaks: list[int]):
    """Halve by stride until the curve fits, keeping the origin and the break markers.

    Dropping the head instead would silently re-base the chart, so the line would stop
    meaning "since inception" while still being labelled that way.
    """
    while len(eq) > MAX_CURVE_POINTS:
        eq = eq[::2]
        bn = bn[::2]
        breaks = sorted({b // 2 for b in breaks})
    return eq, bn, breaks


def summary() -> dict:
    conn = connect()
    q = lambda s: conn.execute(s).fetchone()[0]
    first = conn.execute("SELECT MIN(started_at) FROM sessions").fetchone()[0]
    return {
        "db": str(DB_PATH),
        "sessions": q("SELECT COUNT(*) FROM sessions"),
        "strategies": q("SELECT COUNT(*) FROM strategies"),
        "fills": q("SELECT COUNT(*) FROM fills"),
        "curve_points": q("SELECT COUNT(*) FROM curve"),
        "gaps": q("SELECT COUNT(*) FROM gaps"),
        "since": first,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2))
