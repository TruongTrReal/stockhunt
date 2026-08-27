"""The broker-side record: `state/alpaca.db`.

Separate from `results/paper.db` on purpose. `paper.db` is the forward-test truth and is
tracked in git *because* it is the record; this holds what a third-party broker did with a
copy of that book, it is regenerable by re-running the mirror, and it is gitignored — the
same call `stockhunt/deskdb.py` makes about `desk.db` next door.

**Three tables, and the third is the whole point.** `cycles` and `targets` say what was
reconciled and why. `orders` says what was sent. `fills` is where Alpaca's execution price
sits next to the desk's own mark for the same decision, and the difference between those
two columns is the number this process exists to produce: what the sandbox's bar-close fill
assumption is worth against a real venue at a real spread.

Stdlib only, like `deskdb`, so nothing here can drag a dependency into a process that only
wanted to read a number out of it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import paper_config

DB_PATH = Path(os.environ.get("STOCKHUNT_ALPACA_DB")
               or (paper_config.HERE / "state" / "alpaca.db"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    cls           TEXT NOT NULL,
    snapshot_at   TEXT,
    desk_equity   REAL,
    alpaca_equity REAL,
    ratio         REAL,
    n_orders      INTEGER NOT NULL DEFAULT 0,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_cycles_cls_at ON cycles(cls, at);

CREATE TABLE IF NOT EXISTS targets (
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    symbol   TEXT NOT NULL,
    target   REAL NOT NULL,
    held     REAL NOT NULL,
    mark     REAL,
    PRIMARY KEY (cycle_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    cycle_id        INTEGER REFERENCES cycles(id),
    at              TEXT NOT NULL,
    cls             TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    desk_mark       REAL,
    notional        REAL,
    target          REAL,
    held            REAL,
    state           TEXT NOT NULL,
    alpaca_order_id TEXT,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_cls_at ON orders(cls, at);

CREATE TABLE IF NOT EXISTS fills (
    client_order_id TEXT PRIMARY KEY,
    at              TEXT NOT NULL,
    cls             TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    desk_mark       REAL,
    slip_bp         REAL,
    alpaca_order_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_cls_at ON fills(cls, at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        _conn = conn
        return conn


def use(path: Path | str) -> None:
    """Point the store at another file. For tests, and for `--db`."""
    global DB_PATH, _conn
    with _lock:
        close()
        DB_PATH = Path(path)


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ------------------------------------------------------------------ writes

def start_cycle(cls: str, *, snapshot_at: str | None, desk_equity: float,
                alpaca_equity: float, ratio: float, dry_run: bool,
                note: str | None = None) -> int:
    conn = connect()
    with _lock:
        cur = conn.execute(
            "INSERT INTO cycles (at, cls, snapshot_at, desk_equity, alpaca_equity, "
            "ratio, dry_run, note) VALUES (?,?,?,?,?,?,?,?)",
            (utcnow(), cls, snapshot_at, desk_equity, alpaca_equity, ratio,
             int(dry_run), note))
        conn.commit()
        return int(cur.lastrowid)


def record_targets(cycle_id: int, plan_targets: dict, held: dict,
                   marks: dict) -> None:
    """The reconciliation, as it stood, for every symbol EITHER side named.

    The union matters: a name the desk has dropped but Alpaca still holds is exactly the
    row somebody will go looking for when they ask why a position was sold, and recording
    only the targets would leave it out of the one table that could answer.
    """
    conn = connect()
    rows = [(cycle_id, sym, float(plan_targets.get(sym, 0.0)),
             float(held.get(sym, 0.0)), marks.get(sym))
            for sym in sorted(set(plan_targets) | set(held))]
    if not rows:
        return
    with _lock:
        conn.executemany(
            "INSERT OR REPLACE INTO targets (cycle_id, symbol, target, held, mark) "
            "VALUES (?,?,?,?,?)", rows)
        conn.commit()


def record_order(cycle_id: int, client_order_id: str, order: dict, *,
                 state: str, alpaca_order_id: str | None = None,
                 reason: str | None = None) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO orders (client_order_id, cycle_id, at, cls, symbol, "
            "side, qty, desk_mark, notional, target, held, state, alpaca_order_id, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client_order_id, cycle_id, utcnow(), order["cls"], order["symbol"],
             order["side"], float(order["qty"]), order.get("mark"),
             order.get("notional"), order.get("target"), order.get("held"),
             state, alpaca_order_id, reason))
        conn.execute("UPDATE cycles SET n_orders = n_orders + 1 WHERE id = ?", (cycle_id,))
        conn.commit()


def record_fill(client_order_id: str, *, cls: str, symbol: str, side: str, qty: float,
                price: float, desk_mark: float | None, at: str | None = None,
                alpaca_order_id: str | None = None) -> None:
    """One execution, with the desk's mark beside it.

    `slip_bp` is signed from the desk's point of view: positive means Alpaca filled at a
    price *worse* than the mark the desk assumed, which is the direction that costs money
    on both sides of the trade. It is stored rather than derived on read so a later change
    to the convention cannot silently reinterpret old rows.
    """
    slip = None
    if desk_mark:
        raw = (price - desk_mark) / desk_mark * 10_000.0
        slip = raw if side == "buy" else -raw
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO fills (client_order_id, at, cls, symbol, side, qty, "
            "price, desk_mark, slip_bp, alpaca_order_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (client_order_id, at or utcnow(), cls, symbol, side, float(qty),
             float(price), desk_mark, slip, alpaca_order_id))
        conn.commit()


# ------------------------------------------------------------------ reads

def open_orders(cls: str | None = None) -> list[dict]:
    """Orders this store sent that it has not yet seen a fill for."""
    conn = connect()
    sql = ("SELECT o.* FROM orders o LEFT JOIN fills f "
           "ON f.client_order_id = o.client_order_id "
           "WHERE f.client_order_id IS NULL AND o.state = 'submitted'")
    args: tuple = ()
    if cls:
        sql += " AND o.cls = ?"
        args = (cls,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY o.at", args)]


def slippage(cls: str | None = None) -> dict:
    """The number this whole process exists to produce: how far Alpaca's real fills sat
    from the bar-close prices the sandbox assumed."""
    conn = connect()
    sql = "SELECT COUNT(*) n, AVG(slip_bp) mean, SUM(qty * price) notional FROM fills " \
          "WHERE slip_bp IS NOT NULL"
    args: tuple = ()
    if cls:
        sql += " AND cls = ?"
        args = (cls,)
    row = conn.execute(sql, args).fetchone()
    return {"fills": row["n"] or 0,
            "mean_slip_bp": round(row["mean"], 3) if row["mean"] is not None else None,
            "notional": round(row["notional"], 2) if row["notional"] else 0.0}


def summary() -> dict:
    conn = connect()
    out = {"db": str(DB_PATH)}
    for table in ("cycles", "targets", "orders", "fills"):
        out[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    return out
