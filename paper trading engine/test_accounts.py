"""Accounts and the v1 schema: the record can hold more than one owner's book.

Run from THIS directory, as `paper api/` does with its own suite::

    ..\\.venv\\Scripts\\python -m pytest test_accounts.py -q

Not part of the root `tests/` suite, which depends on numpy and pandas only and must not
need `paper_config` — importing that pulls in the backtest engine, the strategies package
and the whole path bootstrap.

What is being protected here is one property: **two accounts trading the identical cell
keep separate books.** Everything else in the manager desk rests on it, and it is exactly
the kind of thing that looks fine until two people are actually on the desk and their
curves have been quietly averaging for a month.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

import paper_config                                                     # noqa: F401


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """A store pointed at a scratch database, reset between tests.

    `store` keeps its connection in a module global, so redirecting `DB_PATH` alone would
    hand the next test the previous test's open handle. Both have to be cleared.
    """
    import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "paper.db")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "_session_id", None)
    return store


# --------------------------------------------------------------------------- identity

def test_sid_puts_the_account_in_front():
    """In front, not behind — a suffix is what truncation eats.

    Nautilus caps `order_id_tag` at 36 characters and `run_paper` slices to fit, so an
    account written at the end is the first thing lost. Two members on one long-named rule
    would then collapse to the same tag and Nautilus rejects the duplicate registration.
    """
    import store
    assert store.sid_for("a7", "spy-1d-ibs") == "a7:spy-1d-ibs"
    assert store.sid_for(store.HOUSE, "spy-1d-ibs").startswith("00:")

    long_rule = "spy-1d-linearreg_angle_200"
    for account in ("a7", "c2"):
        tag = store.sid_for(account, long_rule)[:36]
        assert tag.startswith(f"{account}:"), "the discriminator must survive truncation"


def test_two_accounts_on_one_cell_keep_separate_books(db):
    """The headline property. Same symbol, same timeframe, same rule, two owners."""
    mine = db.sid_for("a7", "spy-1d-ibs")
    theirs = db.sid_for("c2", "spy-1d-ibs")
    assert mine != theirs

    for sid in (mine, theirs):
        db.upsert_strategy(sid, symbol="SPY", cls="us_stocks", tf="1d", rule="IBS",
                           kind="member", capital=10_000.0)
    db.record_fill(mine, "2026-08-14T00:00:00+00:00", "BUY", 10, 100.0, symbol="SPY")
    db.record_fill(theirs, "2026-08-14T00:00:00+00:00", "BUY", 99, 100.0, symbol="SPY")

    assert db.fill_count(mine) == 1
    assert db.fill_count(theirs) == 1
    assert db.recent_fills(mine)[0]["qty"] == 10
    assert db.recent_fills(theirs)[0]["qty"] == 99


def test_account_column_is_derived_from_the_sid(db):
    """They are two spellings of one fact and must never be able to disagree."""
    db.upsert_strategy(db.sid_for("a7", "spy-1d-ibs"), symbol="SPY", kind="member")
    rows = db.strategies_for("a7")
    assert len(rows) == 1 and rows[0]["account"] == "a7"
    assert db.strategies_for("c2") == []
    assert len(db.strategies_for()) == 1, "None means every account"


# ------------------------------------------------------------------- the fill key

def test_a_replayed_fill_still_deduplicates(db):
    """Warm-up replay re-emits fills. Widening the key must not weaken that."""
    sid = db.sid_for("00", "spy-1d-sma_200")
    db.upsert_strategy(sid, symbol="SPY")
    for _ in range(3):
        db.record_fill(sid, "2026-08-14T00:00:00+00:00", "BUY", 10, 100.0, symbol="SPY")
    assert db.fill_count(sid) == 1


def test_two_symbols_at_one_price_are_two_fills(db):
    """The reason `symbol` joined the key.

    A member strategy holding two names can buy the same size at the same price on the
    same bar. Under the old key those were one row and half the position vanished.
    """
    sid = db.sid_for("a7", "pairs")
    db.upsert_strategy(sid, symbol="", kind="member")
    ts = "2026-08-14T00:00:00+00:00"
    db.record_fill(sid, ts, "BUY", 10, 100.0, symbol="SPY")
    db.record_fill(sid, ts, "BUY", 10, 100.0, symbol="QQQ")
    assert db.fill_count(sid) == 2
    assert {f["symbol"] for f in db.recent_fills(sid)} == {"SPY", "QQQ"}


def test_identical_member_fills_stay_two_rows(db):
    """A manager can send the same order twice on one bar — same symbol, side, size and
    price. Those are two real fills, and collapsing them loses half the position.

    The venue's trade id is what separates them, and it is passed only on the order-driven
    path: a rule-driven strategy needs the opposite behaviour, below.
    """
    sid = db.sid_for("a7", "meanrev")
    db.upsert_strategy(sid, symbol="SPY", kind="member")
    ts = "2026-08-14T00:00:00+00:00"
    db.record_fill(sid, ts, "BUY", 5, 100.0, symbol="SPY", ref="trade-1")
    db.record_fill(sid, ts, "BUY", 5, 100.0, symbol="SPY", ref="trade-2")
    assert db.fill_count(sid) == 2


def test_a_house_replay_still_collapses(db):
    """The opposite requirement, and the one `test_store.py` has always protected: a rule
    strategy re-emits its fills on restart, and those must not double-count. It passes no
    `ref`, so the natural key behaves exactly as it did before `ref` existed."""
    sid = db.sid_for("00", "spy-1d-sma_200")
    db.upsert_strategy(sid, symbol="SPY")
    for _ in range(3):
        db.record_fill(sid, "2026-08-14T00:00:00+00:00", "BUY", 5, 100.0, symbol="SPY")
    assert db.fill_count(sid) == 1


def test_the_same_member_fill_reported_twice_still_collapses(db):
    """`ref` weakens deduplication only for genuinely different fills. One fill delivered
    twice carries one trade id and stays one row."""
    sid = db.sid_for("a7", "meanrev")
    db.upsert_strategy(sid, symbol="SPY", kind="member")
    for _ in range(3):
        db.record_fill(sid, "2026-08-14T00:00:00+00:00", "BUY", 5, 100.0,
                       symbol="SPY", ref="trade-1")
    assert db.fill_count(sid) == 1


def test_a_blank_symbol_does_not_silently_disable_dedup(db):
    """SQLite treats NULLs as distinct in a UNIQUE index, so the column defaults to ''
    rather than NULL. A caller that omits the symbol must still deduplicate."""
    sid = db.sid_for("00", "spy-1d-sma_200")
    db.upsert_strategy(sid, symbol="SPY")
    db.record_fill(sid, "2026-08-14T00:00:00+00:00", "BUY", 10, 100.0)
    db.record_fill(sid, "2026-08-14T00:00:00+00:00", "BUY", 10, 100.0)
    assert db.fill_count(sid) == 1


# --------------------------------------------------------------------- the migration

def _legacy_db(path: Path) -> None:
    """A database in the pre-account shape, with a session's worth of history in it."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                               started_at TEXT NOT NULL, ended_at TEXT, pid INTEGER);
        CREATE TABLE strategies (
            sid TEXT PRIMARY KEY, symbol TEXT, cls TEXT, tf TEXT, rule TEXT, venue TEXT,
            capital REAL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            bt_ir REAL, bt_years REAL, note TEXT);
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL,
            session_id INTEGER NOT NULL, ts TEXT NOT NULL, side TEXT NOT NULL,
            qty REAL NOT NULL, price REAL NOT NULL, book_pnl REAL,
            UNIQUE(sid, ts, side, qty, price));
        CREATE TABLE curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL,
            session_id INTEGER NOT NULL, ts TEXT NOT NULL,
            equity_pct REAL NOT NULL, bench_pct REAL NOT NULL, UNIQUE(sid, ts));
        CREATE TABLE gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL,
            from_ts TEXT NOT NULL, to_ts TEXT NOT NULL, bench_pct REAL,
            UNIQUE(sid, from_ts, to_ts));

        INSERT INTO sessions (started_at) VALUES ('2026-08-01T00:00:00+00:00');
        INSERT INTO strategies (sid, symbol, cls, tf, rule, venue, capital,
                                first_seen, last_seen)
            VALUES ('spy-1d-sma_200','SPY','us_stocks','1d','SMA_200','SANDBOX',10000.0,
                    '2026-08-01T00:00:00+00:00','2026-08-13T00:00:00+00:00');
        INSERT INTO fills (sid, session_id, ts, side, qty, price, book_pnl)
            VALUES ('spy-1d-sma_200',1,'2026-08-02T00:00:00+00:00','BUY',10,100.0,0.0);
        INSERT INTO curve (sid, session_id, ts, equity_pct, bench_pct)
            VALUES ('spy-1d-sma_200',1,'2026-08-02T00:00:00+00:00',1.5,1.2);
        INSERT INTO gaps (sid, from_ts, to_ts, bench_pct)
            VALUES ('spy-1d-sma_200','2026-08-03T00:00:00+00:00',
                    '2026-08-04T00:00:00+00:00',0.4);
    """)
    conn.commit()
    conn.close()


def test_legacy_history_migrates_whole(db, tmp_path):
    """The existing forward record becomes the house's, intact and still joined up."""
    _legacy_db(db.DB_PATH)
    conn = db.connect()

    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION

    sid = "00:spy-1d-sma_200"
    assert [r[0] for r in conn.execute("SELECT sid FROM strategies")] == [sid]
    # The prefix has to reach every table: a sid is a foreign key in all but name, and
    # renaming `strategies` alone orphans every fill, point and gap ever recorded.
    for table in ("fills", "curve", "gaps"):
        assert [r[0] for r in conn.execute(f"SELECT sid FROM {table}")] == [sid], table

    row = conn.execute("SELECT account, kind FROM strategies").fetchone()
    assert row == ("00", "house_rule")

    # Backfilled from `strategies`, not left blank — see below for why that matters.
    assert conn.execute("SELECT symbol FROM fills").fetchone()[0] == "SPY"

    assert db.fill_count(sid) == 1
    assert db.lifetime_curve(sid)["equity"], "the curve survived the rename"


def test_backfilled_symbol_prevents_a_double_count_after_restart(db):
    """The subtle half of the migration.

    A restart replays warm-up bars and the strategy re-emits fills it already reported —
    now WITH a symbol. If the migration had left the legacy rows' symbol blank, the new
    row would miss the old one on the natural key and the fill would be counted twice.
    """
    _legacy_db(db.DB_PATH)
    db.connect()
    sid = "00:spy-1d-sma_200"
    assert db.fill_count(sid) == 1
    db.record_fill(sid, "2026-08-02T00:00:00+00:00", "BUY", 10, 100.0, symbol="SPY")
    assert db.fill_count(sid) == 1, "the replayed fill was counted a second time"


def test_migration_is_idempotent(db):
    """It runs from `connect()`, which runs constantly. Twice must equal once."""
    _legacy_db(db.DB_PATH)
    db.connect()
    db._conn.close()
    db._conn = None
    conn = db.connect()          # a second process opening the same file
    assert [r[0] for r in conn.execute("SELECT sid FROM strategies")] == \
        ["00:spy-1d-sma_200"], "the prefix was applied twice"


def test_a_later_schema_bump_does_not_prefix_a_second_time(db):
    """The trap every future migration will walk into.

    Adding a column bumps `SCHEMA_VERSION`, which re-enters `_migrate` on a database that
    is ALREADY account-scoped. The sid prefix is the one step with nothing to test for, so
    unguarded it runs again: `00:00:spy-1d-sma_200`, and every fill, curve point and gap
    orphaned for a second time. This asserts the version guard, not the current version —
    it must keep passing at v3, v4 and beyond.
    """
    _legacy_db(db.DB_PATH)
    db.connect()
    assert [r[0] for r in db._conn.execute("SELECT sid FROM strategies")] == \
        ["00:spy-1d-sma_200"]

    # Pretend a future release adds a column and bumps the version.
    db._conn.execute("PRAGMA user_version = 1")
    db._migrate(db._conn)

    sids = [r[0] for r in db._conn.execute("SELECT sid FROM strategies")]
    assert sids == ["00:spy-1d-sma_200"], f"double-prefixed: {sids}"
    for table in ("fills", "curve", "gaps"):
        assert all(s[0].count(":") == 1
                   for s in db._conn.execute(f"SELECT sid FROM {table}")), table


def test_a_fresh_database_is_born_migrated(db):
    conn = db.connect()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
    scols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)")}
    assert {"account", "kind", "benchmark"} <= scols
    assert "symbol" in {r[1] for r in conn.execute("PRAGMA table_info(fills)")}


def test_verify_passes_after_migrating_and_catches_a_broken_one(db, monkeypatch):
    """`migrate_owner --verify` has to actually fail on a half-migrated database,
    otherwise it is a green light that means nothing."""
    import migrate_owner
    monkeypatch.setattr(migrate_owner.store, "DB_PATH", db.DB_PATH)

    _legacy_db(db.DB_PATH)
    db.connect()
    assert migrate_owner.verify() == []

    # Orphan a fill by hand — the exact shape of a prefix applied to some tables only.
    db._conn.execute("UPDATE fills SET sid = 'zz:nobody'")
    db._conn.commit()
    problems = migrate_owner.verify()
    assert any("point at no strategy" in p for p in problems), problems
