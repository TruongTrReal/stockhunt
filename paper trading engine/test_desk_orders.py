"""The rules that decide whether a manager's order is allowed to move money.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_desk_orders.py -q

`desk_orders` imports no `nautilus_trader`, which is the point: these are the checks that
bind, and they should be exhaustively testable in milliseconds rather than only reachable
by building a trading node and waiting for a bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import paper_config                                                     # noqa: F401
import desk_orders

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def reg(**kw):
    base = {"strategy_id": "str_a7_meanrev", "account": "a7", "tf": "1d",
            "symbols": ["SPY", "QQQ"], "state": "live", "want": "live",
            "allow_short": 0, "capital": 10_000.0}
    base.update(kw)
    return base


def order(**kw):
    base = {"strategy_id": "str_a7_meanrev", "account": "a7", "action": "new",
            "symbol": "SPY", "side": "buy", "qty": 10, "order_type": "market",
            "limit_price": None, "tif": "day",
            "submitted_at": NOW.isoformat(timespec="seconds")}
    base.update(kw)
    return base


def book(cash=10_000.0, **units):
    return {"cash": cash, "units": dict(units)}


# ------------------------------------------------------------------------ the happy path

def test_a_plain_buy_is_allowed():
    ok, why = desk_orders.validate(order(), reg(), book(), 100.0, NOW)
    assert ok, why


def test_a_sell_of_what_is_held_is_allowed():
    ok, why = desk_orders.validate(order(side="sell", qty=5), reg(), book(SPY=5.0),
                                   100.0, NOW)
    assert ok, why


def test_a_cancel_skips_the_book_checks():
    """A cancel has no symbol, side or size, and refusing it for lack of cash would
    trap a manager in a position they are trying to get out of."""
    ok, why = desk_orders.validate(
        order(action="cancel", symbol=None, side=None, qty=None, order_type=None),
        reg(), book(cash=0.0), None, NOW)
    assert ok, why


# ----------------------------------------------------------------------------- staleness

def test_an_order_older_than_its_bar_is_rejected():
    """The failure being prevented: the desk was down, the queue held the order, and
    filling it now trades a decision made against a price that has gone."""
    old = order(submitted_at=(NOW - timedelta(hours=30)).isoformat(timespec="seconds"))
    ok, why = desk_orders.validate(old, reg(tf="1d"), book(), 100.0, NOW)
    assert not ok
    assert "stale" in why and "fresh order" in why


def test_a_short_restart_rejects_nothing():
    """Two minutes of downtime must not throw away a manager's orders."""
    recent = order(submitted_at=(NOW - timedelta(minutes=2)).isoformat(timespec="seconds"))
    assert desk_orders.validate(recent, reg(tf="4h"), book(), 100.0, NOW)[0]


def test_the_window_follows_the_timeframe():
    """Three hours is alive on a daily strategy and dead on a 4-hour one — the bar is
    the unit the signal was computed on, so it is the unit that expires."""
    o = order(submitted_at=(NOW - timedelta(hours=5)).isoformat(timespec="seconds"))
    assert desk_orders.validate(o, reg(tf="1d"), book(), 100.0, NOW)[0] is True
    assert desk_orders.validate(o, reg(tf="4h"), book(), 100.0, NOW)[0] is False


def test_the_window_is_tunable_without_touching_code():
    o = order(submitted_at=(NOW - timedelta(hours=30)).isoformat(timespec="seconds"))
    assert not desk_orders.validate(o, reg(tf="1d"), book(), 100.0, NOW)[0]
    assert desk_orders.validate(o, reg(tf="1d"), book(), 100.0, NOW, stale_bars=3)[0]


def test_a_naive_timestamp_is_read_as_utc():
    """Everything in this repo writes ISO-8601 UTC, but a hand-written row or an older
    client can arrive without an offset. Guessing local time would shift the staleness
    window by hours in whichever direction the box happens to sit."""
    o = order(submitted_at="2026-08-14T11:59:00")
    assert desk_orders.validate(o, reg(), book(), 100.0, NOW)[0]


# -------------------------------------------------------------------------- the money

def test_a_buy_beyond_the_cash_is_refused():
    """No leverage on this path. Arriving at margin by accident is how a paper track
    record stops meaning anything."""
    ok, why = desk_orders.validate(order(qty=200), reg(), book(cash=1_000.0), 100.0, NOW)
    assert not ok
    assert "not enough cash" in why and "no margin" in why.lower()


def test_a_limit_buy_is_costed_at_its_limit():
    """A limit order can only fill at its limit or better, so that is what it can cost.
    Costing it at the last close would refuse an order that is affordable by construction."""
    o = order(order_type="limit", limit_price=50.0, qty=100)
    ok, why = desk_orders.validate(o, reg(), book(cash=6_000.0), 500.0, NOW)
    assert ok, why


def test_selling_more_than_is_held_is_refused():
    ok, why = desk_orders.validate(order(side="sell", qty=10), reg(), book(SPY=3.0),
                                   100.0, NOW)
    assert not ok and "allow_short" in why


def test_shorting_is_allowed_when_it_was_declared():
    ok, why = desk_orders.validate(order(side="sell", qty=10),
                                   reg(allow_short=1), book(SPY=0.0), 100.0, NOW)
    assert ok, why


def test_a_strategy_sized_book_is_used_not_the_venue_account():
    """Each strategy holds its own cash. Two strategies with 10k each must not be able
    to spend 20k between them because they share a Nautilus venue account."""
    rich = desk_orders.validate(order(qty=90), reg(), book(cash=10_000.0), 100.0, NOW)
    poor = desk_orders.validate(order(qty=90), reg(), book(cash=500.0), 100.0, NOW)
    assert rich[0] is True and poor[0] is False


# ------------------------------------------------------------------------ the shape

@pytest.mark.parametrize("bad,expect", [
    ({"symbol": "TSLA"}, "not one of this strategy's symbols"),
    ({"side": "long"}, "side must be one of"),
    ({"order_type": "stop"}, "type must be one of"),
    ({"qty": 0}, "greater than zero"),
    ({"qty": -5}, "greater than zero"),
    ({"order_type": "limit", "limit_price": None}, "needs a limit_price"),
])
def test_malformed_orders_are_refused_with_a_usable_reason(bad, expect):
    ok, why = desk_orders.validate(order(**bad), reg(), book(), 100.0, NOW)
    assert not ok and expect in why


def test_no_price_yet_is_a_refusal_not_a_crash():
    """Before the first bar for a symbol arrives the desk cannot cost anything. Saying so
    beats guessing, and beats a traceback inside a Nautilus callback."""
    ok, why = desk_orders.validate(order(), reg(), book(), None, NOW)
    assert not ok and "not received a bar" in why


# -------------------------------------------------------------------- strategy lifecycle

@pytest.mark.parametrize("state,want,expect", [
    ("live", "paused", "paused"),
    ("live", "retired", "retired"),
    ("retired", "live", "retired"),
    ("rejected", "live", "rejected"),
])
def test_orders_are_refused_unless_the_strategy_is_running(state, want, expect):
    ok, why = desk_orders.validate(order(), reg(state=state, want=want), book(),
                                   100.0, NOW)
    assert not ok and expect in why


# --------------------------------------------------------------------------- partition

def test_partition_splits_a_batch_and_says_why():
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=1_000.0)}
    prices = {"SPY": 100.0}
    batch = [
        order(qty=5),                                   # affordable
        order(qty=500),                                 # too dear
        order(strategy_id="str_zz_ghost"),              # unknown strategy
    ]
    ok, bad = desk_orders.partition(batch, regs, books, prices, NOW)
    assert len(ok) == 1 and len(bad) == 2
    reasons = [r for _, r in bad]
    assert any("not enough cash" in r for r in reasons)
    assert any("no such strategy" in r for r in reasons)


def test_an_order_for_an_unknown_strategy_is_rejected_not_skipped():
    """Skipping would leave it pending forever, and a queue that never empties looks
    exactly like one that is working."""
    ok, bad = desk_orders.partition([order(strategy_id="ghost")], {}, {}, {}, NOW)
    assert ok == [] and len(bad) == 1


# ------------------------------------------------------------- buying-power reservation
#
# A batch is validated before any of it reaches the exchange, so every order in it would
# otherwise be checked against the same starting cash.

def test_a_batch_cannot_collectively_overspend():
    """Ten individually affordable buys must not add up to more than the cash.

    This is the failure reservation exists to prevent, and it is silent without it: each
    order passes its own check and the strategy ends up holding four times its capital.
    """
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=1_000.0)}
    batch = [order(client_order_id=f"c{i}", qty=4) for i in range(10)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 2, "only 2 x 400 fits in 1,000"
    assert all("not enough cash" in r for _, r in bad)


def test_buy_then_sell_in_one_batch_is_coherent():
    """A manager will do this on their first day. Validating the sell against a book that
    has not seen the buy refuses it for a reason that is untrue by execution time."""
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=10_000.0)}
    batch = [order(client_order_id="b", side="buy", qty=5),
             order(client_order_id="s", side="sell", qty=3)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 2 and bad == []


def test_selling_more_than_the_batch_bought_is_still_refused():
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=10_000.0)}
    batch = [order(client_order_id="b", side="buy", qty=5),
             order(client_order_id="s", side="sell", qty=9)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 1 and len(bad) == 1
    assert "holds 5" in bad[0][1]


def test_a_sell_frees_cash_for_a_later_buy():
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=0.0, SPY=10.0)}
    batch = [order(client_order_id="s", side="sell", qty=10),
             order(client_order_id="b", side="buy", qty=9)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 2, bad


def test_reservation_does_not_leak_between_strategies():
    """Two managers in one batch must not be able to spend each other's cash."""
    regs = {"str_a7_one": reg(strategy_id="str_a7_one"),
            "str_c2_two": reg(strategy_id="str_c2_two", account="c2")}
    books = {"str_a7_one": book(cash=1_000.0), "str_c2_two": book(cash=1_000.0)}
    batch = [order(strategy_id="str_a7_one", client_order_id="a", qty=9),
             order(strategy_id="str_c2_two", client_order_id="b", qty=9)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 2 and bad == []


def test_reservation_never_touches_the_real_book():
    """It is optimistic — the strategy's actual cash only moves on a real fill, and the
    next tick reads it back. A partition that mutated the caller's dict would double-count
    every reservation on the following tick."""
    regs = {"str_a7_meanrev": reg()}
    original = book(cash=10_000.0, SPY=1.0)
    books = {"str_a7_meanrev": original}
    desk_orders.partition([order(qty=5)], regs, books, {"SPY": 100.0}, NOW)
    assert original["cash"] == 10_000.0 and original["units"]["SPY"] == 1.0


def test_a_cancel_reserves_nothing():
    regs = {"str_a7_meanrev": reg()}
    books = {"str_a7_meanrev": book(cash=100.0)}
    batch = [order(client_order_id="x", action="cancel", symbol=None, side=None,
                   qty=None, order_type=None),
             order(client_order_id="b", qty=1)]
    ok, bad = desk_orders.partition(batch, regs, books, {"SPY": 100.0}, NOW)
    assert len(ok) == 2, bad
