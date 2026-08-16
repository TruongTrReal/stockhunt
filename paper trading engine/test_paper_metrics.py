"""The figures the board prints: marking, age and turnover.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_paper_metrics.py -q

Every case here is a number that was on the dashboard and was wrong. None of them needs a
bar, a vendor or the real `paper.db` — they are arithmetic over a registered strategy, so
they run in milliseconds and cannot go stale when somebody refetches a ticker.

The thread joining them: **the desk is books end to end, and three of these figures were
written for a system that holds one instrument.** A book registers with `symbol` set to a
label ("5 names"), keeps its quantities in `holdings`, and resumes an inception date from
the store — so a price lookup by `symbol`, a fill counter scoped to the process and an age
measured from the first bar of this session all silently produced nothing.
"""

from __future__ import annotations

import sqlite3
import pytest

import paper_config                                                     # noqa: F401
import store
import paper_state


@pytest.fixture()
def desk(tmp_path):
    """A registry backed by a throwaway store. The real `paper.db` is never opened."""
    store.close() if hasattr(store, "close") else None
    store._conn = None
    store.DB_PATH = tmp_path / "paper.db"
    paper_state.STATE_PATH = tmp_path / "paper_state.json"
    paper_state.MIRROR_PATH = None
    paper_state._strategies.clear()
    yield paper_state
    try:
        store._conn.close()
    except Exception:
        pass
    store._conn = None


def book(ps, sid="00:us_stocks-1d-ibs", capital=100_000.0, **kw):
    """Register a book the way `book_strategy.on_start` does."""
    fields = dict(
        account="00", kind="book", symbol="3 names", venue="SANDBOX", cls="us_stocks",
        tf="1d", rule="ibs", benchmark=None, state="flat", status="warming",
        since="2026-08-14", days=0, paper_pnl_pct=0.0, paper_trades=0,
        position_units=0, entry=None, capital=capital, cash=capital, units=0.0,
        equity=capital, turnover=0.0, note="", held=0, names=3,
        holdings=[{"symbol": "AAPL", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0},
                  {"symbol": "MSFT", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0},
                  {"symbol": "NVDA", "state": "flat", "units": 0.0, "entry": None,
                   "mark": None, "value": 0.0, "pnl_pct": None, "trades": 0}])
    fields.update(kw)
    ps.register(sid, **fields)
    return ps._strategies[sid]


def backdate(ps, sid, iso):
    """Age a system by moving the STORE's inception, then re-registering.

    `store.upsert_strategy` stamps `first_seen` from the wall clock and ignores whatever
    `since` the caller passes — correctly, since inception is when the desk first saw the
    system, not what a config says. So an old system can only be modelled by writing the
    store row, which is also exactly what a restart reads back.
    """
    conn = store.connect()
    conn.execute("UPDATE strategies SET first_seen = ? WHERE sid = ?", (iso, sid))
    conn.commit()
    fields = dict(ps._strategies[sid])
    fields.pop("id", None)
    ps.register(sid, **fields)
    return ps._strategies[sid]


# --------------------------------------------------------------------------- marking

def test_a_book_is_revalued_between_bars(desk):
    """The headline number on the whole desk, and it never moved.

    `mark()` looked the strategy's `symbol` up in the price dict. A book's is the string
    "3 names", so the lookup missed, the book was skipped, and P&L only changed when a bar
    closed — on a daily system, once every 24 hours. Every system on this desk is a book,
    so `mark()` returned 0 on every tick and the board sat at 0.000% exactly as its own
    docstring warns.
    """
    s = book(desk)
    s.update(cash=40_000.0, held=2, state="long")
    s["holdings"][0].update(units=100.0, entry=500.0)      # $50,000 in at 500
    s["holdings"][1].update(units=50.0, entry=200.0)       # $10,000 in at 200

    marked = desk.mark({"AAPL": 550.0, "MSFT": 200.0, "NVDA": 900.0})

    assert marked == 1, "the book was skipped entirely"
    # 40,000 cash + 100x550 + 50x200 = 105,000
    assert s["equity"] == pytest.approx(105_000.0)
    assert s["paper_pnl_pct"] == pytest.approx(5.0)


def test_marking_a_book_updates_the_rows_it_is_made_of(desk):
    """The total and the expanded table are drawn from the same prices, or they disagree
    on screen and the reader cannot tell which is stale."""
    s = book(desk)
    s.update(cash=40_000.0, held=1)
    s["holdings"][0].update(units=100.0, entry=500.0)

    desk.mark({"AAPL": 550.0, "MSFT": 200.0, "NVDA": 900.0})

    held = s["holdings"][0]
    assert held["mark"] == 550.0
    assert held["value"] == pytest.approx(55_000.0)
    assert held["pnl_pct"] == pytest.approx(10.0)
    assert s["holdings"][2]["pnl_pct"] is None, "a flat name has no entry to measure from"


def test_a_held_name_with_no_price_marks_nothing(desk):
    """Half a book is worse than none of it: the total would be short by a whole position
    and would read as a loss the desk did not take."""
    s = book(desk)
    s.update(cash=40_000.0, held=2)
    s["holdings"][0].update(units=100.0, entry=500.0)
    s["holdings"][1].update(units=50.0, entry=200.0)

    assert desk.mark({"AAPL": 550.0}) == 0
    assert s["equity"] == 100_000.0, "left at its last honest value"


def test_a_single_instrument_system_still_marks(desk):
    """The path that always worked, kept working."""
    s = book(desk, sid="00:crypto-1d-sma", kind="rule", symbol="BTC/USD", names=1,
             holdings=None)
    s.update(cash=50_000.0, units=2.0)

    assert desk.mark({"BTC/USD": 30_000.0}) == 1
    assert s["equity"] == pytest.approx(110_000.0)
    assert s["mark_price"] == 30_000.0


# ------------------------------------------------------------------------------- age

def test_days_comes_from_inception_not_from_this_process(desk, monkeypatch):
    """`since` is resumed from the store; `days` was measured from the first bar of the
    session. The board printed both, side by side: "since 2026-08-14 · 1 day in", days
    later. The correction existed but was guarded on the caller passing a LATER `since` —
    and `_export`, the path that runs every bar, passes no `since` at all.
    """
    from datetime import datetime, timezone

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(paper_state, "datetime", Now)
    s = book(desk)
    s = backdate(desk, s["id"], "2026-08-14T00:00:00+00:00")
    desk.update(s["id"], days=1, paper_pnl_pct=0.0)     # what the strategy reports

    assert s["days"] == 10, "ten days since 2026-08-14, not one since the restart"
    assert s["since"] == "2026-08-14"


# -------------------------------------------------------------------------- turnover

def test_turnover_is_round_trips_per_name_per_year(desk, monkeypatch):
    """`book_strategy` set it to 0.0 at registration and never touched it again, so a desk
    with 1,389 fills on it reported "turnover 0.0/yr" on every row."""
    from datetime import datetime, timezone

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 8, 14, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(paper_state, "datetime", Now)
    s = book(desk)
    s = backdate(desk, s["id"], "2026-08-14T00:00:00+00:00")   # exactly one year old
    assert s["days"] == 365
    for i in range(60):                              # 60 fills = 30 round trips
        desk.push_trade(s["id"], f"2026-09-{i % 28 + 1:02d} 00:00", "BUY", 1.0, 100.0,
                        symbol="AAPL", ref=f"f{i}")

    assert s["lifetime_trades"] == 60
    # 60 fills / 2 = 30 round trips, over one year, across 3 names
    assert s["turnover"] == pytest.approx(10.0, abs=0.05)


def test_turnover_is_zero_without_fills(desk):
    s = book(desk)
    assert s["turnover"] == 0.0
