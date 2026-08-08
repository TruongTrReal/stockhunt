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

import os
import sqlite3
import threading
from datetime import datetime, timezone

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

CREATE TABLE IF NOT EXISTS strategies (
    sid         TEXT PRIMARY KEY,
    symbol      TEXT, cls TEXT, tf TEXT, rule TEXT, venue TEXT,
    capital     REAL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    bt_ir       REAL, bt_years REAL, note TEXT
);

-- The natural key is the fill itself. Two genuinely distinct fills for one strategy at the
-- same bar time, same side, same size and same price are indistinguishable and would not
-- be separable by any key that does not come from the venue -- and on a replay the venue
-- ids are regenerated, so they are no help. Collapsing them is the safe direction: a
-- double-counted fill corrupts the record, a dropped duplicate does not.
CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT NOT NULL,
    session_id  INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    book_pnl    REAL,
    UNIQUE(sid, ts, side, qty, price)
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
    with _lock:
        conn.execute("""
            INSERT INTO strategies (sid, symbol, cls, tf, rule, venue, capital,
                                    first_seen, last_seen, bt_ir, bt_years, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sid) DO UPDATE SET
                symbol=excluded.symbol, cls=excluded.cls, tf=excluded.tf,
                rule=excluded.rule, venue=excluded.venue, capital=excluded.capital,
                last_seen=excluded.last_seen, bt_ir=excluded.bt_ir,
                bt_years=excluded.bt_years, note=excluded.note
        """, (sid, f.get("symbol"), f.get("cls"), f.get("tf"), f.get("rule"),
              f.get("venue"), f.get("capital"), now, now, f.get("bt_ir"),
              f.get("bt_years"), f.get("note")))
        conn.commit()
        row = conn.execute("SELECT first_seen FROM strategies WHERE sid = ?",
                           (sid,)).fetchone()
    return row[0] if row else now


def record_fill(sid: str, ts: str, side: str, qty: float, price: float,
                book_pnl: float = 0.0) -> None:
    conn = connect()
    sess = session_id()          # resolved BEFORE the lock: it may open a session, which
    with _lock:                  # takes the same non-reentrant lock and would deadlock
        conn.execute("""INSERT OR IGNORE INTO fills
                        (sid, session_id, ts, side, qty, price, book_pnl)
                        VALUES (?,?,?,?,?,?,?)""",
                     (sid, sess, ts, side, float(qty), float(price), float(book_pnl)))
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
    conn = connect()
    rows = conn.execute("""SELECT ts, side, qty, price, book_pnl FROM fills
                           WHERE sid = ? ORDER BY ts DESC, id DESC LIMIT ?""",
                        (sid, limit)).fetchall()
    return [{"ts": r[0], "side": r[1], "qty": r[2], "price": r[3],
             "pnl": round(r[4] or 0.0, 2)} for r in reversed(rows)]


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
