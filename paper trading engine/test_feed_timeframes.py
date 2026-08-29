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

import pandas as pd
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
    why = desk_control._feed_caveat("cme_futures", "1m")
    assert why.startswith("Running"), "it must not read as a refusal"
    assert "behind real time" in why and "fill PRICE" in why


@pytest.mark.parametrize("cls,tf", [("us_stocks", "1m"), ("cme_futures", "1h"),
                                    ("crypto", "5m"), ("cme_futures", "1d")])
def test_nothing_else_carries_a_caveat(cls, tf):
    """`reason` beside `live` has to stay rare, or it stops being read at all."""
    import desk_control
    assert desk_control._feed_caveat(cls, tf) == ""


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
    ctl._underwater = set()
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


def test_now_is_read_on_the_bar_s_own_clock():
    """The same defect as the commodity one, in the opposite and more expensive direction.

    An equity's cache is `America/New_York`, so `_to_cache_clock` correctly leaves its
    stamps there — and the guard then compared that ET stamp against a UTC `now` four or
    five hours AHEAD of it. `now < open + duration` was therefore essentially never true,
    the guard never fired, and the desk KEPT the still-forming bar: a rule computed from a
    high, a low and a close that had not finished happening. Discarding a good bar costs a
    session; trading one that has not closed is look-ahead.
    """
    import pandas as pd

    et = td_live._now_in_cache_clock("AAPL")
    utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    assert et.tzinfo is None, "the bars are naive, so the comparison has to be too"
    assert round((utc - et).total_seconds() / 3600) in (4, 5), (
        "an equity's clock is New York, and it is four or five hours behind UTC")

    # ...and it is exactly the identity everywhere the cache is already UTC, so no other
    # class's behaviour moves. `ts_event` is derived from these stamps and `ts` is part of
    # the fills table's natural key, so an accidental shift here would double the record.
    for symbol in ("BTC/USD", "XAU/USD", "ES.v.0", "NOT-A-TICKER"):
        assert abs((td_live._now_in_cache_clock(symbol) - utc).total_seconds()) < 5


def test_a_forming_equity_bar_is_dropped_and_a_closed_one_is_kept():
    """The guard itself, on the two instants either side of a real bar's close."""
    import pandas as pd

    bar = pd.Timestamp("2026-08-27 15:55:00")            # ET, the vendor's own stamp
    frame = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                          "Close": [1.0], "Volume": [1.0]}, index=[bar])

    def guard_at(when_et: str, symbol: str = "AAPL", tf: str = "5m"):
        stamp = pd.Timestamp(when_et)
        real = td_live._now_in_cache_clock
        td_live._now_in_cache_clock = lambda s: stamp
        try:
            return td_live._without_forming(frame, symbol, tf)
        finally:
            td_live._now_in_cache_clock = real

    assert guard_at("2026-08-27 15:57:00").empty, "15:57 is inside the 15:55 bar"
    assert len(guard_at("2026-08-27 16:00:00")) == 1, "at 16:00 the bar has closed"
    assert len(guard_at("2026-08-27 16:05:00")) == 1


def test_a_restamp_fixes_a_label_and_a_wide_bar_needs_its_grid_rebuilt():
    """Commodity 4h is the one cell the live desk may not fetch from the vendor.

    A whole-hour restamp moves a bar's label and leaves `1m`..`1h` covering the same
    windows, so those are fetched. `4h` is wider than the offset — Sydney is UTC+10/+11,
    `10 % 4 == 2` — so a restamped 4h bar lands off the grid `data/commodities/4h` holds,
    and off a DIFFERENT one in summer than in winter. The cache was rebuilt from 1h by
    `migrate_cache_clock.py`; this is the same rebuild on the live path, which is what
    stops the desk trading bars that are in no research sheet.
    """
    assert td_live.derived_from("XAU/USD", "4h") == "1h"
    for tf in ("1m", "5m", "15m", "30m", "1h"):
        if tf in td_live.INTERVALS:
            assert td_live.derived_from("XAU/USD", tf) is None
    assert td_live.derived_from("XAU/USD", "1d") is None, (
        "a daily commodity bar is the vendor's own 21:00 UTC roll-up, a third convention")
    for symbol in ("SPY", "AAPL", "BTC/USD", "ES.v.0", "NOT-A-TICKER"):
        assert td_live.derived_from(symbol, "4h") is None


def test_the_derived_grid_is_the_cache_s_grid():
    """Built by the same function that wrote the cache, so the two cannot drift apart."""
    import pandas as pd
    import resample_intraday

    idx = pd.date_range("2026-08-27 00:00", periods=48, freq="1h")
    hourly = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
                           "Volume": 10.0}, index=idx)
    out = resample_intraday.resample_frame(hourly, 240)
    assert sorted(set(out.index.hour)) == [0, 4, 8, 12, 16, 20]
    assert list(out.columns) == list(hourly.columns)


# --------------------------------------------- can this book afford one unit of that?
#
# The failure that produced this was invisible for hours. A member pointed TradingView at
# `cme_futures` with the standard $10,000 book, sending `{{strategy.order.contracts}}` —
# an INTEGER. On this leg a unit is a fractional notional unit of a back-adjusted series,
# so one NQ.v.0 is ~$29,600. Every order was refused for want of cash, correctly and with
# a well-worded reason — written onto the ORDER row, which nobody reads until they already
# suspect something. The registration itself just said `live`.

def _reg_for(cls, symbols, capital, tf="1d"):
    return {"cls": cls, "tf": tf, "symbols": list(symbols), "capital": capital}


def test_a_book_that_cannot_afford_a_unit_is_told_so(monkeypatch):
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "NQ.v.0": pd.DataFrame({"Close": [29_600.0]}),
    })
    why = desk_control._affordability_caveat(
        _reg_for("cme_futures", ["NQ.v.0"], 10_000.0))
    assert "29,600" in why and "10,000" in why
    assert "0.34" in why, "say how much it CAN hold, not just that it cannot hold one"
    assert "fractional notional unit, not a contract" in why


def test_a_book_that_can_afford_it_is_told_nothing(monkeypatch):
    """This column is only useful while it stays rare."""
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "NQ.v.0": pd.DataFrame({"Close": [29_600.0]}),
    })
    assert desk_control._affordability_caveat(
        _reg_for("cme_futures", ["NQ.v.0"], 500_000.0)) == ""


def test_it_names_the_DEAREST_symbol_and_lists_every_unaffordable_one(monkeypatch):
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "ES.v.0": pd.DataFrame({"Close": [7_700.0]}),      # affordable
        "NQ.v.0": pd.DataFrame({"Close": [29_600.0]}),
        "YM.v.0": pd.DataFrame({"Close": [53_700.0]}),     # the dearest
    })
    why = desk_control._affordability_caveat(
        _reg_for("cme_futures", ["ES.v.0", "NQ.v.0", "YM.v.0"], 10_000.0))
    assert "one unit of YM.v.0" in why, "the binding constraint is the dearest one"
    assert "NQ.v.0, YM.v.0" in why and "ES.v.0" not in why.split("—")[1]


def test_a_broken_caveat_RAISES_here_and_is_caught_where_it_can_be_logged(monkeypatch):
    """Non-fatal and LOUD, in that order — and this test exists because of a real miss.

    The helper used to wrap itself in `except Exception: return ""`. While writing it, a
    `NameError` in this file's own stub was swallowed and the function returned "" for
    three runs, which reads exactly like "this book can afford it". A courtesy that must
    not fail an attach is right; a courtesy that hides a programming error is the silent
    success this module is full of warnings about.

    So the helper raises and `_attach` catches, logs and continues — the catch is where a
    logger exists.
    """
    import desk_control
    import td_loader

    def boom(*a, **k):
        raise RuntimeError("cache gone")

    monkeypatch.setattr(td_loader, "load", boom)
    with pytest.raises(RuntimeError):
        desk_control._affordability_caveat(_reg_for("cme_futures", ["NQ.v.0"], 10_000.0))


def test_no_symbols_or_no_capital_says_nothing(monkeypatch):
    import desk_control
    assert desk_control._affordability_caveat(_reg_for("us_stocks", [], 10_000.0)) == ""
    assert desk_control._affordability_caveat(_reg_for("us_stocks", ["SPY"], 0.0)) == ""


# ------------------------------------------------- ...and how far the book may lever
#
# The affordability caveat above measures against BUYING POWER, which stopped being the
# capital the day leverage became a member's choice. It is the mechanism that told this
# desk's owner, correctly, that a $10,000 book cannot hold 2 NQ; told to a $10,000 book at
# 4x it would be false, and a caveat that contradicts a setting its reader just chose is
# one they learn to ignore.

def test_the_affordability_caveat_counts_leverage(monkeypatch):
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "NQ.v.0": pd.DataFrame({"Close": [29_600.0]}),
    })
    reg = _reg_for("cme_futures", ["NQ.v.0"], 10_000.0)
    assert desk_control._affordability_caveat(reg), "unlevered, it cannot afford one"
    # 4x on $10,000 is $40,000 of buying power, which carries one unit with room to spare.
    assert desk_control._affordability_caveat(dict(reg, leverage=4.0)) == ""


def test_the_caveat_names_both_numbers_when_they_differ(monkeypatch):
    """Saying "this book is $10,000" on a levered registration understates its reach by the
    leverage, which is the one number the reader has just chosen and will check."""
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "YM.v.0": pd.DataFrame({"Close": [53_700.0]}),
    })
    why = desk_control._affordability_caveat(
        dict(_reg_for("cme_futures", ["YM.v.0"], 10_000.0), leverage=2.0))
    assert "10,000 at 2x" in why and "20,000 of buying power" in why
    assert "0.37" in why, "the size it can hold is the LEVERED one"


def test_an_unlevered_caveat_reads_exactly_as_it_always_did(monkeypatch):
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "NQ.v.0": pd.DataFrame({"Close": [29_600.0]}),
    })
    why = desk_control._affordability_caveat(
        dict(_reg_for("cme_futures", ["NQ.v.0"], 10_000.0), leverage=1.0))
    assert "this book is $10,000, so" in why and "buying power" not in why


# --------------------------------------------------- and what the desk will not run
#
# `desk_orders` bounds an ORDER; this bounds a REGISTRATION, and it is the only place the
# ceiling is read. The API bounds it too, earlier and more kindly, but a ledger row is
# untrusted input whatever the API said about it.

def test_a_registration_above_the_ceiling_is_refused():
    import desk_control
    import paper_config
    over = paper_config.max_leverage("us_stocks") + 1
    why = desk_control._leverage_refusal(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="member", leverage=over))
    # The number, and what it costs. A refusal that only says "too much" teaches nothing
    # about the scale of what was asked for, and 125x is the owner's number rather than a
    # venue's — so the row cannot lean on "your broker would not allow this" any more.
    assert "ceiling" in why and "125x" in why and "0.8%" in why


def test_a_registration_at_the_ceiling_is_run():
    import desk_control
    import paper_config
    at = paper_config.max_leverage("us_stocks")
    assert desk_control._leverage_refusal(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="member", leverage=at)) == ""


def test_one_range_for_every_class(monkeypatch):
    """**The inversion of what this file used to assert**, and it is the owner's call.

    Crypto was capped at 1.0 — spot crypto is not marginable for US retail and
    `alpaca_mirror`, this desk's second record, extends no crypto margin — and futures at
    10. The range is 1x to 125x on every class now, and the crypto fact is not lost: it is
    written down in `paper_config.MAX_LEVERAGE` as what a levered crypto book costs, which
    is that the broker-side mirror stops being able to copy it.
    """
    import desk_control
    import paper_config
    seen = {paper_config.max_leverage(c) for c in paper_config.UNIVERSE}
    assert seen == {125.0}
    for cls, sym in (("crypto", "BTC/USD"), ("cme_futures", "ES.v.0"),
                     ("commodities", "XAU/USD"), ("us_etfs", "SPY")):
        assert desk_control._leverage_refusal(
            dict(_reg_for(cls, [sym], 10_000.0), kind="member", leverage=125.0)) == ""


def test_the_caveat_carries_the_wipeout_distance():
    """`0.8%` at 125x is the honest number for what the setting costs, and it is
    arithmetic — 100/leverage — rather than an opinion. It replaces the old per-class
    sentence, which could lean on a venue's margin rules and no longer can."""
    import desk_control
    why = desk_control._leverage_caveat(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="member", leverage=125.0))
    assert "125x" in why and "0.8%" in why and "ZERO" in why
    # ...and at a modest setting the same sentence is unalarming and still true, which is
    # what makes it safe to print on every levered row.
    mild = desk_control._leverage_caveat(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="member", leverage=2.0))
    assert "50%" in mild
    # Never on an unlevered book: this column is only useful while it stays rare.
    assert desk_control._leverage_caveat(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="member", leverage=1.0)) == ""


def test_the_wipeout_arithmetic_is_one_definition():
    import paper_config
    assert paper_config.wipeout_move_pct(125) == pytest.approx(0.8)
    assert paper_config.wipeout_move_pct(2) == pytest.approx(50.0)
    assert paper_config.wipeout_move_pct(1) == pytest.approx(100.0)


def test_a_promoted_rule_may_not_be_levered_at_all():
    """A house rule and a book are selected off `wf_summary_*`, and every sheet in this
    repo scores an UNLEVERED book. They also do not use the order path, so a levered one
    would not merely be incomparable — it would be unenforced."""
    import desk_control
    why = desk_control._leverage_refusal(
        dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind="house_rule", leverage=2.0))
    assert "cannot be levered" in why and "wf_summary_us_stocks_1d" in why


def test_an_unlevered_registration_of_any_kind_is_never_refused():
    import desk_control
    for kind in ("member", "house_rule", "book"):
        assert desk_control._leverage_refusal(
            dict(_reg_for("us_stocks", ["SPY"], 10_000.0), kind=kind)) == ""


# --------------------------------------------------- and when there is nothing left
#
# `desk_orders` refuses every exposure-adding order once equity reaches zero, which is
# correct and completely silent: the registration still reads `live`, bars still arrive,
# and the owner's only symptom is that their orders stop working one refusal at a time.

class _Broke:
    """The smallest thing `_watch_equity` reads: a book's marks and its equity."""

    def __init__(self, equity, priced=True, leverage=2.0):
        self._equity, self._priced = equity, priced

        class _C:
            tf = "1d"
            symbols = ("SPY",)
        self.config = _C()
        self.config.leverage = leverage

    def prices(self):
        return {"SPY": 100.0} if self._priced else {"SPY": 0.0}

    def equity(self):
        return self._equity


def _watched(monkeypatch, strategy):
    """Run one equity pass and return every `(state, reason)` it wrote to the ledger."""
    import desk_control
    from stockhunt import deskdb
    written = []
    monkeypatch.setattr(deskdb, "mark_registration",
                        lambda rid, state, reason=None: written.append((state, reason)))
    ctl = _controller(_Cache([]))
    # `_watch_equity` filters on MemberStrategy, so the stub has to be one to be seen.
    monkeypatch.setattr(desk_control, "MemberStrategy", _Broke)
    ctl._running = {"rid": strategy}
    ctl._watch_equity()
    return ctl, written


def test_a_book_at_zero_equity_says_so_on_its_own_row(monkeypatch):
    ctl, written = _watched(monkeypatch, _Broke(0.0))
    assert "rid" in ctl._underwater
    state, reason = written[-1]
    assert state == "live", "it can still CLOSE, which is the one thing it must be able to"
    assert "REDUCE" in reason and "2x" in reason


def test_a_solvent_book_is_not_accused_of_anything(monkeypatch):
    ctl, written = _watched(monkeypatch, _Broke(9_000.0))
    assert ctl._underwater == set() and written == []


def test_it_is_reported_once_and_cleared_when_the_book_recovers(monkeypatch):
    import desk_control
    from stockhunt import deskdb
    written = []
    monkeypatch.setattr(deskdb, "mark_registration",
                        lambda rid, state, reason=None: written.append(reason))
    monkeypatch.setattr(desk_control, "MemberStrategy", _Broke)
    ctl = _controller(_Cache([]))
    broke = _Broke(-500.0)
    ctl._running = {"rid": broke}

    ctl._watch_equity()
    ctl._watch_equity()                          # a second pass must say nothing new
    assert len(written) == 1

    broke._equity = 250.0
    ctl._watch_equity()
    assert written[-1] is None and ctl._underwater == set()


def test_an_unfed_book_is_left_to_the_feed_watch(monkeypatch):
    """Before its first bar a book is worth its cash and nothing is measurable. Two
    sentences on one row for one problem is worse than one."""
    ctl, written = _watched(monkeypatch, _Broke(0.0, priced=False))
    assert ctl._underwater == set() and written == []


def test_neither_watch_erases_the_other(monkeypatch):
    """`reason` is one column and both watches write it. Two independent writers meant the
    second to fire overwrote the first, and either one CLEARING itself wrote None over the
    other's live warning — so a book that was both unfed and broke stopped reporting either
    the moment one of them recovered."""
    import desk_control
    from stockhunt import deskdb
    written = []
    monkeypatch.setattr(deskdb, "mark_registration",
                        lambda rid, state, reason=None: written.append(reason))
    monkeypatch.setattr(desk_control, "MemberStrategy", _Broke)
    ctl = _controller(_Cache([]))
    ctl._running = {"rid": _Broke(0.0)}
    ctl._quiet.add("rid")                        # as if the feed watch had already fired

    ctl._watch_equity()
    assert "no 1d bar has arrived" in written[-1]
    assert "REDUCE" in written[-1]


# ============================================================ symbols from any class
#
# **A registration is already a portfolio and was confined to one asset class**, not
# because of anything about the book but because `cls` on the row decided the venue, the
# instrument shape and therefore the vendor for every symbol it named. Since 2026-08-29
# class is a property of the SYMBOL and `cls` is the book's HOME leg: what the board files
# it under, and the fallback for a name the desk cannot place on its own.
#
# The failure this replaces is not loud. A `BTC/USD` registered alongside equities would
# have been built as a whole-share `Equity` on `SANDBOX`, admitted to `CLASS_OF` as
# `us_stocks`, and -- worst -- a futures name would have gone to the Twelve Data side of
# `run_paper._split_by_feed`, where `ES` is Eversource Energy and comes back as a clean,
# plausible, entirely wrong series.

def test_symbol_classes_maps_each_name_to_its_own_leg():
    import desk_control
    held = desk_control.symbol_classes(
        _reg_for("us_stocks", ["SPY", "BTC/USD", "XAU/USD", "ES.v.0"], 10_000.0))
    # Every one of these is on a PINNED leg, so `CLASS_OF` answers without a vendor.
    assert held == {"SPY": "us_etfs", "BTC/USD": "crypto",
                    "XAU/USD": "commodities", "ES.v.0": "cme_futures"}


def test_a_single_class_registration_is_unchanged():
    """The backwards-compatibility property, and it is the one worth stating first: every
    registration written before this existed must resolve exactly as it did."""
    import desk_control
    assert desk_control.symbol_classes(
        _reg_for("us_stocks", ["AAPL", "MSFT"], 10_000.0)) == {
            "AAPL": "us_stocks", "MSFT": "us_stocks"}


def test_an_unknown_symbol_falls_back_to_the_declared_class():
    """Which is exactly the old behaviour. The desk resolves it for real at attach; until
    then the registration's own class is the only thing anybody has said about it."""
    import desk_control
    assert desk_control.symbol_classes(
        _reg_for("crypto", ["ZZ-NOT-A-SYMBOL"], 10_000.0)) == {
            "ZZ-NOT-A-SYMBOL": "crypto"}


class _Member:
    """A stand-in `self` for `MemberStrategy`'s per-symbol lookups.

    The METHODS under test are the real ones — they are bound to this object below — and
    only the two attributes they read are supplied. `Strategy.__init__` wants a Nautilus
    kernel and `self.config` is not a plain instance attribute on it, so a real instance
    cannot be assembled without a trading node; a test that had to build one to check a
    dictionary lookup is a test nobody runs.
    """

    def __init__(self, config, pairs):
        self.config = config
        self._classes = dict(pairs)

    def __getattr__(self, name):
        from member_strategy import MemberStrategy
        return getattr(MemberStrategy, name).__get__(self, type(self))


def _member(cls, symbols, pairs, venue="SANDBOX"):
    from member_strategy import MemberStrategyConfig
    cfg = MemberStrategyConfig(
        registration_id="str_a7_x", account="a7", name="x", cls=cls, tf="1d",
        venue=venue, symbols=tuple(symbols), symbol_classes=tuple(pairs))
    return _Member(cfg, pairs)


def test_each_symbol_gets_its_own_venue_and_instrument_shape():
    """The whole reason class had to move off the registration. One book, three venues,
    three instrument shapes -- and the venue is what routes the DATA, so getting it wrong
    for a futures name asks Twelve Data for a CME contract."""
    strat = _member("us_stocks", ["SPY", "BTC/USD", "ES.v.0"],
                    [("SPY", "us_etfs"), ("BTC/USD", "crypto"),
                     ("ES.v.0", "cme_futures")])
    assert strat._venue_of("SPY") == "SANDBOX"
    assert strat._venue_of("BTC/USD") == "BINANCE"
    assert strat._venue_of("ES.v.0") == "GLBX"
    assert strat.classes() == ["cme_futures", "crypto", "us_etfs"]
    assert strat.venues() == ["BINANCE", "GLBX", "SANDBOX"]
    # The instrument shape follows the class, not the spelling. `XAU/USD` and `BTC/USD`
    # carry the same separator and settle on different venues; `ES.v.0` is not a share.
    assert str(strat._instrument_id("SPY")) == "SPY.SANDBOX"
    assert str(strat._instrument_id("BTC/USD")) == "BTCUSD.BINANCE"
    assert str(strat._instrument_id("ES.v.0")) == "ES.v.0.GLBX"


def test_an_empty_symbol_class_map_means_the_registrations_own_class():
    """What every row written before this field existed carries. It must not need a
    migration: an absent map and a map naming `cls` for everything are one behaviour."""
    strat = _member("crypto", ["BTC/USD"], [], venue="BINANCE")
    assert strat._class_of("BTC/USD") == "crypto"
    assert strat._venue_of("BTC/USD") == "BINANCE"
    assert str(strat._instrument_id("BTC/USD")) == "BTCUSD.BINANCE"


def test_the_config_stays_hashable_with_a_class_map():
    """`StrategyConfig` is a FROZEN msgspec Struct, so it has a generated `__hash__` over
    its fields. A dict field would make the whole config unhashable at whatever point
    Nautilus first hashes it -- nowhere near this line, and not obviously about it. Pairs."""
    strat = _member("us_stocks", ["SPY"], [("SPY", "us_etfs")])
    assert isinstance(hash(strat.config), int)
    assert strat.config.id                     # ...and it still encodes to JSON


def test_a_mixed_book_is_refused_whole_and_the_symbol_is_named(monkeypatch):
    """**The deliberate half of the feedability decision.** `cme_futures` cannot be fed at
    4h -- the GLBX ohlcv archive has no such schema -- and a mixed book holding one such
    symbol could either be refused whole or have that leg dropped.

    Dropping is refused: silently trading four of somebody's five symbols is a book that
    is not the book they registered, and nothing downstream -- the curve, the P&L, the
    record -- would say which one it is. So the answer is no, and it NAMES the symbol,
    because a refusal that only names a class the registration may not even mention sends
    the owner looking in the wrong place.
    """
    import desk_control
    import db_live
    monkeypatch.setattr(db_live, "have_key", lambda: True)
    reg = _reg_for("us_stocks", ["SPY", "ES.v.0"], 10_000.0, tf="4h")
    held = desk_control.symbol_classes(reg)
    problems = []
    for cls in sorted(set(held.values()) | {reg["cls"]}):
        can, why = desk_control._feedable(cls, reg["tf"])
        if not can:
            named = sorted(s for s, c in held.items() if c == cls)
            problems.append(f"{why} ({', '.join(named)})")
    assert len(problems) == 1
    assert "ES.v.0" in problems[0] and "4h" in problems[0]


def test_the_feed_caveat_reaches_a_mixed_book(monkeypatch):
    """A book filed under `us_stocks` that also holds `ES.v.0` at 1m runs half of itself
    off Databento's archive. A caveat keyed on the row's `cls` would have said nothing
    about it, which is this column's whole failure mode one class over."""
    import desk_control
    import db_live
    monkeypatch.setattr(db_live, "feed_mode", lambda: "poll")
    monkeypatch.setattr(db_live, "FEED_MODE", {"why": "no SDK on this box"})
    reg = dict(_reg_for("us_stocks", ["SPY", "ES.v.0"], 10_000.0, tf="1m"),
               kind="member", leverage=1.0)
    why = desk_control._caveat(reg)
    assert "HISTORICAL archive" in why


def test_the_affordability_caveat_prices_each_class_from_its_own_cache(monkeypatch):
    """`td_loader.load` reads `data/<class>/1d/`, so a mixed book has to be loaded per
    class. Asking for every symbol under the registration's own class returns empty frames
    for the ones that live elsewhere, which reads as "nothing to say" -- and saying
    something is this function's whole job."""
    import desk_control
    import td_loader
    asked = []

    def fake_load(cls, tf, syms):
        asked.append((cls, tuple(syms)))
        if cls == "cme_futures":
            return {"ES.v.0": pd.DataFrame({"Close": [29_600.0]})}
        return {"SPY": pd.DataFrame({"Close": [500.0]})}

    monkeypatch.setattr(td_loader, "load", fake_load)
    why = desk_control._affordability_caveat(
        dict(_reg_for("us_stocks", ["SPY", "ES.v.0"], 10_000.0), leverage=1.0))
    assert ("cme_futures", ("ES.v.0",)) in asked
    assert "ES.v.0" in why and "29,600" in why


def test_the_affordability_caveat_still_fires_at_the_new_floor(monkeypatch):
    """The floor is $1,000 and the rounding argument that set it at $10,000 is WORSE
    there, not better. It is not blocked; it is made visible, here, at attach."""
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "SPY": pd.DataFrame({"Close": [5_000.0]}),
    })
    why = desk_control._affordability_caveat(
        dict(_reg_for("us_etfs", ["SPY"], 1_000.0), leverage=1.0))
    assert "5,000" in why and "this book is $1,000" in why
    assert "0.20" in why


def test_leverage_lets_a_small_book_afford_what_it_otherwise_could_not(monkeypatch):
    """A $1,000 book at 125x can carry things a $1,000 book cannot, and a caveat that
    contradicted the member's own leverage setting would teach them to ignore caveats."""
    import desk_control
    import td_loader
    monkeypatch.setattr(td_loader, "load", lambda cls, tf, syms: {
        "SPY": pd.DataFrame({"Close": [5_000.0]}),
    })
    assert desk_control._affordability_caveat(
        dict(_reg_for("us_etfs", ["SPY"], 1_000.0), leverage=125.0)) == "", \
        "$125,000 of buying power carries a $5,000 unit; there is nothing to warn about"


# ------------------------------------------------------- funding a mixed book's venues

def test_a_mixed_book_funds_every_venue_it_touches_at_full_size():
    """**The decision, written down.** A registration's cash is ONE pot, so the whole book
    can legitimately be deployed into any single one of its venues at any moment -- a
    split would under-fund exactly the case somebody registered a mixed book to run.
    Over-funding a sandbox account costs nothing and is invisible: every system sizes
    against its own book, never the venue balance."""
    import run_paper
    known = {"SANDBOX": 0.0, "BINANCE": 0.0, "SPOT": 0.0, "GLBX": 0.0}
    reg = dict(_reg_for("us_stocks", ["SPY", "BTC/USD"], 10_000.0), kind="member")
    assert run_paper._venues_of(reg, known) == ["BINANCE", "SANDBOX"]


def test_an_unresolved_symbol_is_credited_to_every_venue():
    """`symbol_resolve` runs at attach, minutes or days after the node is built, so an
    unadmitted name has no venue here. Guessing one and being wrong is a book that stops
    filling partway through with nothing raised anywhere."""
    import run_paper
    known = {"SANDBOX": 0.0, "BINANCE": 0.0, "SPOT": 0.0, "GLBX": 0.0}
    reg = dict(_reg_for("us_etfs", ["ZZ-NOT-ADMITTED"], 10_000.0), kind="member")
    assert run_paper._venues_of(reg, known) == ["BINANCE", "GLBX", "SANDBOX", "SPOT"]


def test_a_book_with_no_symbols_funds_its_own_class():
    """A `book` names nothing -- it holds whoever is in its class right now -- so its
    class really is the answer there."""
    import run_paper
    known = {"SANDBOX": 0.0, "BINANCE": 0.0}
    assert run_paper._venues_of({"cls": "crypto", "symbols": []}, known) == ["BINANCE"]


def test_the_funding_headroom_is_not_the_strategy_ceiling():
    """They were one constant and had to be split. A headroom of 100,000 books at $10,000
    is $1bn before the doubling, and a Nautilus `Money` refuses anything above
    9,223,372,036 -- so the node would have failed to BUILD, on every start."""
    import desk_control
    assert desk_control.FUNDING_HEADROOM_STRATEGIES == 60
    assert desk_control.MAX_MEMBER_STRATEGIES >= 100_000


def test_the_member_ceiling_is_still_a_check_that_can_refuse():
    """The mechanism is kept and the number is not a limit. A bug that used to stop at 60
    would otherwise fill the ledger with nothing anywhere to stop it, and this refusal is
    the only evidence such a loop ever produces."""
    import inspect
    import desk_control
    src = inspect.getsource(desk_control.DeskController._launch)
    assert "max_member_strategies" in src and "ceiling" in src
