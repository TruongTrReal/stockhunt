"""Every timeframe the desk OFFERS is a timeframe it can actually subscribe to.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_feed_timeframes.py -q

This is the regression test for a silent failure that cost fifteen hours of a forward
test. `td_nautilus.timeframe_of` had two hardcoded branches, `1d` and `4h`, while
`paper_config.MEMBER_TIMEFRAMES` offered six and `/v1/limits` advertised all six. A member
registering at `5m` therefore got:

    201 from the API              the timeframe IS in MEMBER_TIMEFRAMES
    the strategy attached         DeskController accepted it for the same reason
    "RUNNING" in the log          Nautilus started it happily
    state = live in the console   nothing had raised where anyone could see
    ValueError, in a task         `_subscribe_bars` failed, logged ERROR, went nowhere

...and then every order that strategy ever sent was refused with *"no price for BTC/USD
yet — try again after the next 5m close"*, which is advice that cannot come true, because
no 5m close was ever going to be delivered to it.

The check that was supposed to prevent exactly this compared `MEMBER_TIMEFRAMES` against
`BAR_SPEC` — which is derived from the BACKTEST engine's timeframe list and says nothing
whatever about what the live vendor client implements. Two lists that both look
authoritative, neither of which was the capability.

So: assert against the feed, and assert the round trip, not just the membership. A
timeframe that maps to the WRONG key polls on the wrong cadence, which is a quieter bug
than the one this file is named for.
"""

from __future__ import annotations

import pytest

import paper_config                                                     # noqa: F401
import td_live
import td_nautilus

from nautilus_trader.model.data import BarType


def _bar_type(tf: str, symbol: str = "BTCUSD.BINANCE") -> BarType:
    return BarType.from_str(f"{symbol}-{paper_config.BAR_SPEC[tf]}")


@pytest.mark.parametrize("tf", paper_config.MEMBER_TIMEFRAMES)
def test_every_offered_timeframe_can_be_fed(tf):
    """The vendor client knows how to ask for it."""
    assert tf in td_live.INTERVALS, (
        f"{tf} is offered to members but td_live cannot fetch it")


@pytest.mark.parametrize("tf", paper_config.MEMBER_TIMEFRAMES)
def test_every_offered_timeframe_round_trips(tf):
    """tf -> Nautilus bar spec -> tf, unchanged.

    The direction that matters is the second one: `_poll` calls `timeframe_of` on the bar
    type it was handed and uses the answer to pick both the vendor interval and the sleep
    to the next close. A spec that maps to a different key polls on the wrong cadence and
    fetches the wrong series, with nothing malformed anywhere to notice.
    """
    assert td_nautilus.timeframe_of(_bar_type(tf)) == tf


@pytest.mark.parametrize("tf", paper_config.BOOK_TIMEFRAMES)
def test_book_timeframes_are_feedable_too(tf):
    """The house's own books run on the same client and must not be forgotten."""
    assert td_nautilus.timeframe_of(_bar_type(tf)) == tf


def test_an_unsupported_spec_still_raises():
    """The mapping widened; it did not become permissive.

    `3m` is a spec Nautilus will happily parse and the vendor does not serve, so it must
    still fail loudly rather than fall through to a neighbouring interval.
    """
    with pytest.raises(ValueError):
        td_nautilus.timeframe_of(BarType.from_str("BTCUSD.BINANCE-3-MINUTE-LAST-EXTERNAL"))


def test_equities_map_the_same_way():
    """The venue is not part of the timeframe, and a regression that made it so would
    show up only on one asset class."""
    for tf in paper_config.MEMBER_TIMEFRAMES:
        assert td_nautilus.timeframe_of(_bar_type(tf, "QQQ.SANDBOX")) == tf


def test_a_resampled_timeframe_is_present_but_not_feedable():
    """PRESENT IS NOT FEEDABLE, and this is the regression that proved it.

    `2m` and `3m` were added to the backtest engine's timeframe table on 2026-08-21 for
    the intraday study. They are RESAMPLED from cached 1m bars and Twelve Data sells no
    such product, so `td_live.INTERVALS` carries a row for each with a vendor interval of
    `None`. A membership test — `key in INTERVALS` — then reports them feedable, and the
    live client gets a bar type it can spell and can never subscribe to.

    That is the fifteen-hour silent failure this module's docstring describes, reached by
    a different route: the strategy attaches, reads `live`, and every order it sends is
    refused for want of a price.
    """
    for tf in ("2m", "3m"):
        assert tf in td_live.INTERVALS, f"{tf} should still be a known timeframe"
        assert td_live.INTERVALS[tf][0] is None, f"{tf} must have no vendor interval"
        step = int(tf.rstrip("m"))
        with pytest.raises(ValueError):
            td_nautilus.timeframe_of(
                BarType.from_str(f"BTCUSD.BINANCE-{step}-MINUTE-LAST-EXTERNAL"))


def test_every_book_timeframe_has_a_real_vendor_interval():
    """The house's own books are no longer all 1d/4h. A book timeframe the client cannot
    feed fails inside a Nautilus task at subscribe time — logged, and going nowhere."""
    for tf in paper_config.BOOK_TIMEFRAMES:
        assert td_live.INTERVALS.get(tf, (None,))[0] is not None, tf


# --------------------------------------------------------------- the 1m budget
#
# `1m` joined `MEMBER_TIMEFRAMES` on 2026-08-28 and is the one size on that list whose cost
# is not amortised by the bar being long: `td_nautilus` polls once per symbol per BAR, so a
# minute book is one vendor request per symbol per minute against the 610/minute budget
# `td_live` is quoted for. `desk_control.MAX_MEMBER_STRATEGIES` is 60 and `SYMBOLS_MAX` is
# 20, so the unguarded worst case is 1,200 requests a minute — which would not degrade the
# offending book, it would take the feed down for every book on the desk.

def test_one_minute_is_offered_and_feedable():
    """The thing the file is named for, applied to the newest member of the list."""
    assert "1m" in paper_config.MEMBER_TIMEFRAMES
    assert "1m" in paper_config.BAR_SPEC
    interval, _ = td_live.INTERVALS["1m"]
    assert interval == "1min", "a real vendor product, not a spelling"


def test_one_minute_is_not_a_book_timeframe():
    """A book holds the WHOLE class, so 1m there is the regime the cap exists to stop.

    `book_universe("us_stocks")` is the live top 100 — one promotion would be 100 requests
    a minute on its own. Members name at most 20 symbols; books name none and take
    everybody.
    """
    assert "1m" not in paper_config.BOOK_TIMEFRAMES


def _reg(symbols, tf="1m", sid="s1"):
    return {"strategy_id": sid, "tf": tf, "symbols": list(symbols), "cls": "us_stocks"}


def test_the_minute_budget_counts_symbols_not_registrations(monkeypatch):
    """Three members on the same twenty tickers cost twenty polls, not sixty.

    Subscriptions are shared by (symbol, timeframe), so counting registrations would
    refuse the cheap case and wave the expensive one through — the exact inversion.
    """
    import desk_control
    from stockhunt import deskdb
    shared = [f"T{i}" for i in range(20)]
    monkeypatch.setattr(deskdb, "active_registrations",
                        lambda: [_reg(shared, sid="a"), _reg(shared, sid="b")])
    ctl = desk_control.DeskController.__new__(desk_control.DeskController)
    assert ctl._minute_budget_exceeded(_reg(shared, sid="c")) == ""


def test_the_minute_budget_refuses_past_the_ceiling(monkeypatch):
    import desk_control
    from stockhunt import deskdb
    already = [f"X{i}" for i in range(paper_config.MAX_1M_SYMBOLS)]
    monkeypatch.setattr(deskdb, "active_registrations",
                        lambda: [_reg(already, sid="a")])
    ctl = desk_control.DeskController.__new__(desk_control.DeskController)
    why = ctl._minute_budget_exceeded(_reg(["NEW"], sid="b"))
    assert why, "one more symbol past the ceiling must be refused"
    assert str(paper_config.MAX_1M_SYMBOLS) in why, "say the number, not just 'no'"


def test_only_one_minute_registrations_count_against_it(monkeypatch):
    """A 5m book of a hundred names is 20 polls a minute and must not consume this."""
    import desk_control
    from stockhunt import deskdb
    big5m = _reg([f"Y{i}" for i in range(200)], tf="5m", sid="a")
    monkeypatch.setattr(deskdb, "active_registrations", lambda: [big5m])
    ctl = desk_control.DeskController.__new__(desk_control.DeskController)
    assert ctl._minute_budget_exceeded(_reg(["AAPL"], sid="b")) == ""


def test_an_unreadable_ledger_fails_CLOSED(monkeypatch):
    """Refuse rather than guess. An admitted-by-accident 1m book is a desk-wide outage."""
    import desk_control
    from stockhunt import deskdb

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(deskdb, "active_registrations", boom)
    ctl = desk_control.DeskController.__new__(desk_control.DeskController)
    why = ctl._minute_budget_exceeded(_reg(["AAPL"]))
    assert "refusing rather than guessing" in why


def test_futures_DO_run_at_one_minute_now():
    """1m was refused on the CME leg until 2026-08-28, and the refusal was over-strict.

    The bar really does arrive late — the desk polls Databento's HISTORICAL archive, whose
    frontier was sampled at 5.5-7.3 minutes behind real time — but a MEMBER strategy does
    not compute its signal from this feed. It arrives over the webhook from TradingView's
    own real-time data; the desk needs a bar to price a fill and mark a book. So the
    honest answer is "yes, and here is what is stale about it", which is a caveat on the
    row rather than a closed door.
    """
    import desk_control
    can, why = desk_control._feedable("cme_futures", "1m")
    assert can and why == ""


def test_accepting_one_minute_futures_still_says_what_is_stale():
    """A caveat, in `reason`, beside `live`. A system that runs, fills and publishes while
    quietly meaning something other than its owner thinks is the failure being avoided."""
    import desk_control
    why = desk_control._caveat("cme_futures", "1m")
    assert why.startswith("Running"), "it must not read as a refusal"
    assert "behind real time" in why and "fill PRICE" in why


@pytest.mark.parametrize("cls,tf", [("us_stocks", "1m"), ("cme_futures", "1h"),
                                    ("crypto", "5m"), ("cme_futures", "1d")])
def test_nothing_else_carries_a_caveat(cls, tf):
    """`reason` beside `live` has to stay rare, or it stops being read at all."""
    import desk_control
    assert desk_control._caveat(cls, tf) == ""


@pytest.mark.parametrize("tf", ["4h", "15m", "5m"])
def test_a_missing_schema_says_so_about_ITSELF(tf):
    """...and the sizes that really have no schema still say that, naming their own."""
    import desk_control
    can, why = desk_control._feedable("cme_futures", tf)
    assert not can
    assert f"no {tf} schema" in why
    assert "stale" not in why, "these are absent, not late — a different problem"


class _Recorder:
    """A stand-in for the controller's logger, so a test can read what it said."""

    def __init__(self):
        self.errors, self.warnings, self.infos = [], [], []

    def error(self, msg):
        self.errors.append(str(msg))

    def warning(self, msg):
        self.warnings.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))


# ------------------------------------------------------- retiring closes the book
#
# `_retire` called `remove_strategy` and nothing else until 2026-08-28. Stopping a strategy
# does NOT close what it holds — neither `BookStrategy.on_stop` nor `MemberStrategy.on_stop`
# ever did — so a retired strategy left an open position at the venue with nothing managing
# it: nothing to mark it, size it or exit it, and no way to reach it again short of
# restarting the node. "Retired" on the console read as flat when it was not.

class _Pos:
    def __init__(self, name):
        self.instrument_id = name
        self.quantity = 1


class _Strategy:
    def __init__(self, sid="s1"):
        self.id = sid
        self.closed = []
        self._sid = None

    def close_position(self, pos):
        self.closed.append(pos.instrument_id)


class _Cache:
    def __init__(self, positions):
        self._positions = positions

    def positions_open(self, strategy_id=None):
        return list(self._positions)


def _controller(cache, log=None):
    """A controller whose `cache` and `log` are ours, WITHOUT touching the real class.

    `cache` and `log` are read-only properties on `Actor`, so a stub has to override them
    — and doing that by assigning to `DeskController.cache` would patch the class every
    other test in this process shares, including the ones that build a real node. A
    throwaway subclass keeps it local.
    """
    import desk_control
    recorder = log or _Recorder()

    class _Stubbed(desk_control.DeskController):
        cache = property(lambda self: cache)
        log = property(lambda self: recorder)

    ctl = _Stubbed.__new__(_Stubbed)
    ctl._running, ctl._attached_at, ctl._quiet = {}, {}, set()
    return ctl


def test_retiring_closes_every_open_position():
    strategy = _Strategy()
    ctl = _controller(_Cache([_Pos("ES.v.0.GLBX"), _Pos("NQ.v.0.GLBX")]))
    assert ctl._flatten("rid", strategy) == 2
    assert strategy.closed == ["ES.v.0.GLBX", "NQ.v.0.GLBX"]


def test_flattening_a_flat_book_does_nothing_and_says_nothing():
    strategy = _Strategy()
    ctl = _controller(_Cache([]))
    assert ctl._flatten("rid", strategy) == 0
    assert strategy.closed == []


def test_one_position_that_will_not_close_does_not_stop_the_others():
    """...and it must not raise: `_flatten` runs inside `tick()`.

    An exception here aborts the pass, so the registration is never marked `retired`, so
    the next tick tries again — forever, with the strategy already gone from `_running`.
    A position that cannot be closed is a thing to report loudly, not a reason to wedge
    the desk.
    """
    class Stubborn(_Strategy):
        def close_position(self, pos):
            if pos.instrument_id == "BAD":
                raise RuntimeError("no market")
            self.closed.append(pos.instrument_id)

    strategy = Stubborn()
    rec = _Recorder()
    ctl = _controller(_Cache([_Pos("BAD"), _Pos("GOOD")]), log=rec)
    assert ctl._flatten("rid", strategy) == 1
    assert strategy.closed == ["GOOD"]
    assert any("STILL OPEN" in e for e in rec.errors), "an unclosed position must shout"


def test_an_unreadable_cache_does_not_raise_either():
    class Boom:
        def positions_open(self, strategy_id=None):
            raise RuntimeError("cache gone")

    ctl = _controller(Boom(), log=_Recorder())
    assert ctl._flatten("rid", _Strategy()) == 0


# ---------------------------------------------- the staleness window is not a table
#
# `desk_orders.BAR_SECONDS` was `{"1d": 86_400, "4h": 14_400}` with a one-day default, and
# a comment claiming nothing else could be registered. That stopped being true when
# `MEMBER_TIMEFRAMES` grew, and the failure was silent in the worst direction: every
# intraday timeframe the desk gained got a ONE DAY staleness window, so a 1m order that
# waited fourteen hours was still "fresh".

@pytest.mark.parametrize("tf,seconds", [("1d", 86_400), ("4h", 14_400), ("2h", 7_200),
                                        ("1h", 3_600), ("15m", 900), ("5m", 300),
                                        ("1m", 60)])
def test_every_offered_timeframe_has_its_OWN_stale_window(tf, seconds):
    import desk_orders
    assert desk_orders.bar_seconds(tf) == seconds
    assert desk_orders.stale_window(tf).total_seconds() == seconds


def test_a_minute_order_goes_stale_in_a_minute_not_a_day():
    """The bug, stated as the thing it allowed."""
    import desk_orders
    from datetime import datetime, timedelta, timezone
    sent = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    order = {"submitted_at": sent.isoformat()}
    assert not desk_orders.is_stale(order, "1m", sent + timedelta(seconds=30))
    assert desk_orders.is_stale(order, "1m", sent + timedelta(minutes=2))
    assert desk_orders.is_stale(order, "1m", sent + timedelta(hours=14))


def test_a_name_that_is_not_a_timeframe_still_falls_back_to_a_day():
    import desk_orders
    assert desk_orders.bar_seconds("bogus") == 86_400
    assert desk_orders.bar_seconds("") == 86_400


# --------------------------------------------------------------------------- the clock
#
# Same family of failure as the one this file is named for, one layer down: the desk and
# the sheet it selects from disagreed about what a timestamp MEANT, and nothing said so.
# Twelve Data stamps commodity intraday bars in `Australia/Sydney` and declares nothing;
# read as UTC they are 10-11 hours in the future, so `fetch_bars`' forming-bar guard never
# fired and the commodity legs ran permanently one bar behind. Silent, and green.

def test_live_bars_land_on_the_same_clock_as_the_backtest_cache():
    import pandas as pd

    idx = pd.DatetimeIndex(["2025-07-15 08:00", "2025-07-15 09:00"])
    for symbol in ("SPY", "BTC/USD", "ES.v.0"):
        assert list(td_live._to_cache_clock(idx, symbol)) == list(idx)
    # Southern-hemisphere winter: Sydney is UTC+10, so the vendor's 08:00 is 22:00 the
    # day before. In January it would be 11 hours, which is why this is a tz conversion
    # and not a subtraction.
    metal = td_live._to_cache_clock(idx, "XAU/USD")
    assert list(metal) == list(idx - pd.Timedelta(hours=10))
    assert list(td_live._to_cache_clock(
        pd.DatetimeIndex(["2025-01-15 08:00"]), "XAU/USD")) == [
            pd.Timestamp("2025-01-14 21:00")]


def test_an_unknown_symbol_does_not_kill_the_process():
    """A member may register anything the vendor prices. `paper_config.class_of` exits on
    a symbol outside the forward-test universe, which is right where the desk uses it and
    fatal on a path that only wants to know a timezone."""
    import pandas as pd

    assert td_live.class_of("NOT-A-TICKER") is None
    idx = pd.DatetimeIndex(["2025-07-15 08:00"])
    assert list(td_live._to_cache_clock(idx, "NOT-A-TICKER")) == list(idx)


def test_the_forming_bar_guard_now_sees_a_commodity_bar_as_past():
    """The measured symptom, as arithmetic rather than as a network call.

    A commodity bar that closed an hour ago is stamped 10-11 hours AHEAD of `now` by the
    vendor. Against that stamp `now < open + duration` is always true, so the newest row
    was dropped on every read. On the cache clock it is behind `now`, and it is kept.
    """
    import pandas as pd
    from datetime import timezone

    now = pd.Timestamp("2025-07-14 23:30", tz="UTC")
    vendor_stamp = pd.DatetimeIndex(["2025-07-15 08:00"])       # = 22:00 UTC, 90m ago
    duration = td_live.INTERVALS["1h"][1]

    raw = vendor_stamp[-1].tz_localize(timezone.utc)
    assert now < raw + duration                                  # dropped, wrongly

    fixed = td_live._to_cache_clock(vendor_stamp, "XAU/USD")[-1].tz_localize(timezone.utc)
    assert now >= fixed + duration                               # kept, correctly
