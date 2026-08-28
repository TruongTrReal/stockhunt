r"""The CME futures leg: the instrument, the feed's refusals, and the roll arithmetic.

Run from THIS directory::

    ..\.venv\Scripts\python -m pytest test_futures_leg.py -q

Offline. Nothing here reaches Databento — the vendor paths are exercised by
`python db_live.py` (the REST archive) and `python db_stream.py` (the LIVE gateway, which
also checks its own bars against the archive's for the same minutes), and neither can be a
unit test, because a test that fails when a vendor is slow is a test nobody will trust.

Six properties, and each one is a failure this folder has already paid for once wearing a
different hat:

* **The instrument is fractional.** `FuturesContract` has no `size_increment`, so a whole
  contract against a $5,263 slice rounds ES to zero and the whole book sits flat while
  every log line reads healthy. Same shape as the crypto one-increment fills.
* **The legs stay disjoint.** `class_of` is a reverse lookup and decides the sheet, the
  venue and the feed. A symbol in two legs resolves to whichever was declared first.
* **An unfeedable timeframe is refused AT SUBSCRIBE TIME.** Present is not feedable: the
  GLBX archive has no 4h or 15m schema at all. A bar type that can be spelled and never
  subscribed to is the fifteen-hour silent failure `test_feed_timeframes.py` is named for.
* **The live roll reproduces the cache's back-adjustment.** Not to the same anchor — the
  live buffer is anchored where its warm-up landed and the cache at its newest bar — but
  to the same series up to one global constant, which is all a scale-equivariant indicator
  can tell apart. Asserted on the POLLED frames and again on the STREAMED ones, because
  the gateway delivers raw continuous prices exactly as the poller's raw fetch does and
  an unadjusted roll on either path hands a strategy a return nobody earned.
* **A dropped stream degrades, loudly, and says which mode it is in.** Every subscription
  the gateway was carrying gets a poll task, `db_live.FEED_MODE` says `poll`, and
  `desk_control._caveat` starts telling members their fills are priced off a stale bar
  again. A silent downgrade to an eight-minute feed is worse than either mode chosen
  deliberately.
* **A reconnect replays the gap.** The SDK's own reconnect policy re-subscribes with
  `start=None` and drops the outage's bars, which is why `db_stream` owns the reconnect
  and asks the gateway to replay from the last bar it saw.
"""

from __future__ import annotations

import asyncio
import time
import types

import numpy as np
import pandas as pd
import pytest

import paper_config
import db_live
import db_nautilus
import db_stream
import td_nautilus

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair, Equity

FUTURES = "cme_futures"


# ------------------------------------------------------------------ the leg

def test_the_leg_exists_and_is_wired_end_to_end():
    """A leg is a feed, an instrument, a venue and a sheet. All four, or it is a list."""
    assert paper_config.UNIVERSE[FUTURES], "no symbols on the futures leg"
    assert paper_config.VENUES[FUTURES] == "GLBX"
    assert FUTURES in paper_config.PAIR_CLASSES
    assert paper_config.has_sheet(FUTURES, "1d"), "no wf_summary to select rules from"
    assert db_live.can_feed("1d")


def test_the_legs_stay_disjoint():
    """`paper_config` raises at import if they are not; this says so where a reader looks.

    The futures spelling is what keeps it true here: `CL.v.0` and not `CL`, which is
    already a member of the equity universe. A bare root would have resolved crude oil to
    a toothpaste company, silently, because `class_of` returns the first claimant.
    """
    seen: dict[str, str] = {}
    for cls, symbols in paper_config.UNIVERSE.items():
        for symbol in symbols:
            assert symbol not in seen, f"{symbol} is in both {seen[symbol]} and {cls}"
            seen[symbol] = cls
    assert paper_config.class_of("ES.v.0") == FUTURES
    assert paper_config.class_of("CL.v.0") == FUTURES
    # `CL` bare is a member of the RESEARCH equity universe. It is not on the desk's own
    # roster, so `CLASS_OF.get` is None here — the point is that it is not crude oil.
    assert paper_config.CLASS_OF.get("CL") != FUTURES


def test_every_futures_symbol_is_a_continuous_contract():
    """Root, roll rule, rank. `ES` names no instrument; `ES.v.0` names a rule for
    picking one, which is the true thing to say about the series."""
    for symbol in paper_config.UNIVERSE[FUTURES]:
        root, rule, rank = symbol.split(".")
        assert root and rule == "v" and rank == "0", symbol


# ------------------------------------------------------------------ the instrument

@pytest.mark.parametrize("symbol", ["ES.v.0", "NKD.v.0", "ZC.v.0", "LE.v.0"])
def test_the_futures_instrument_is_fractional_and_on_glbx(symbol):
    inst = td_nautilus.futures_instrument(symbol, "GLBX")
    assert isinstance(inst, CurrencyPair), (
        "a FuturesContract has no size_increment, so its quantities are whole contracts")
    assert inst.id.venue == Venue("GLBX")
    assert inst.id.symbol == Symbol(symbol), "the desk symbol travels unmangled"
    assert float(inst.size_increment) < 1.0, "a whole-unit book rounds ES to zero"
    assert inst.base_currency.code == symbol.split(".")[0]
    assert inst.quote_currency.code == "USD"


def test_a_slice_of_the_book_is_a_tradable_quantity():
    """The arithmetic the fractional instrument exists for, stated as a number.

    $100,000 across nineteen names is ~$5,263 a slice. One REAL E-mini S&P contract is 50
    index points at ~7,700, which is ~$385,000 of exposure — `futures_specs.CME_CONTRACTS`
    carries the multiplier — so a whole-contract instrument rounds that slice to zero and
    the leg holds nothing while every log line reads healthy.
    """
    import futures_specs
    inst = td_nautilus.futures_instrument("ES.v.0", "GLBX")
    slice_usd = paper_config.BOOK_CAPITAL / len(paper_config.UNIVERSE[FUTURES])
    contract_usd = 7_700.0 * futures_specs.CME_CONTRACTS["ES"]["qty"]
    assert round(slice_usd / contract_usd) == 0, "a whole contract is unaffordable here"
    assert float(inst.make_qty(slice_usd / 7_700.0)) > 0.0


def test_the_symbol_round_trips_through_nautilus():
    """Dotted symbols are safe: `ES.v.0.GLBX` parses back to symbol and venue, and a bar
    type built from it survives `from_str`, which is how every subscription is spelled."""
    iid = InstrumentId(Symbol("ES.v.0"), Venue("GLBX"))
    bar_type = BarType.from_str(f"{iid}-1-DAY-LAST-EXTERNAL")
    assert bar_type.instrument_id.symbol.value == "ES.v.0"
    assert bar_type.instrument_id.venue == Venue("GLBX")
    assert str(BarType.from_str(str(bar_type))) == str(bar_type)


def test_one_dispatcher_decides_the_shape_for_every_class():
    """Five call sites used to carry this branch. The futures leg needed a third arm in
    all of them, which is exactly the shape of thing that ends up right in four."""
    assert isinstance(td_nautilus.instrument_for("QQQ", "us_etfs", "SANDBOX"), Equity)
    assert isinstance(td_nautilus.instrument_for("BTC/USD", "crypto", "BINANCE"),
                      CurrencyPair)
    futures = td_nautilus.instrument_for("ES.v.0", FUTURES, "GLBX")
    assert futures.id == InstrumentId(Symbol("ES.v.0"), Venue("GLBX"))
    assert float(futures.size_increment) < 1.0


def test_the_vendor_symbol_of_a_future_is_itself():
    """`SAFE_TO_VENDOR` only holds the separator-carrying pairs, so a dotted root falls
    through as identity — which is what Databento's continuous symbology wants."""
    iid = InstrumentId(Symbol("ES.v.0"), Venue("GLBX"))
    assert td_nautilus.vendor_symbol(iid) == "ES.v.0"


# ------------------------------------------------------------------ what the feed refuses

@pytest.mark.parametrize("tf", ["1d", "1h", "1m"])
def test_the_servable_schemas_round_trip(tf):
    """`1m` joined on 2026-08-28. `ohlcv-1m` was always a real schema — it is what
    `data/futures/1m` was fetched from — and the objection to feeding it live was the
    archive's ~7-minute lag, which is a caveat to state rather than a capability to
    withhold. See `desk_control._caveat`."""
    spec = paper_config.BAR_SPEC[tf]
    assert db_nautilus.timeframe_of(
        BarType.from_str(f"ES.v.0.GLBX-{spec}")) == tf


@pytest.mark.parametrize("spec", ["4-HOUR-LAST-EXTERNAL", "15-MINUTE-LAST-EXTERNAL",
                                  "5-MINUTE-LAST-EXTERNAL"])
def test_an_unservable_timeframe_is_refused(spec):
    """The GLBX ohlcv archive has no 4h, 15m or 5m schema AT ALL. The research sheets at
    those sizes were cut from cached 1m bars offline, which a live poll cannot ask for —
    a different problem from 1m's, which exists and is merely late."""
    with pytest.raises(ValueError):
        db_nautilus.timeframe_of(BarType.from_str(f"ES.v.0.GLBX-{spec}"))


def test_the_fallback_poll_lag_clears_the_measured_frontier():
    """The fixed lag is now only reached when the frontier cannot be read at all.

    It has to clear the measured WORST case and not the mean: the archive frontier is a
    sawtooth that advances in ~10-minute steps, so a lag inside it finds nothing settled
    and the bar is skipped in silence. `ARCHIVE_LAG_SECONDS` is the worst reading taken
    (13.0 min, 2026-08-28); if somebody re-measures it upward this fails rather than the
    desk quietly polling too early.
    """
    for tf in ("1m", "1h", "1d"):
        assert db_nautilus.poll_lag(tf) >= db_live.ARCHIVE_LAG_SECONDS, (
            "the fallback lag must clear the archive frontier or the bar is never settled")


def test_the_frontier_wait_is_bounded_below_the_retry_loop():
    """A stalled archive must not wedge a poll task, and must not eat its own backstop.

    Both halves matter. Without a ceiling the wait is unbounded; with a ceiling ABOVE the
    retry loop the wait would consume the cover the retry loop is there to provide, and
    the failure would be a bar silently skipped rather than a warning.
    """
    assert db_nautilus.FRONTIER_MAX_WAIT < (db_nautilus.MAX_RETRIES
                                            * db_nautilus.RETRY_EVERY)
    assert db_nautilus.FRONTIER_FLOOR < db_nautilus.FRONTIER_MAX_WAIT
    # ...and the floor stays inside the SMALLEST lag ever observed (3.5 min), or the first
    # question is asked after the answer could already have been yes.
    assert db_nautilus.FRONTIER_FLOOR < 3.5 * 60


def test_the_frontier_wait_asks_the_archive_and_stops_when_it_arrives(monkeypatch):
    """The bar is fetched when the archive SAYS it has it, not when a constant expires."""
    asked = []
    ends = [pd.Timestamp("2026-08-28 12:00"), pd.Timestamp("2026-08-28 12:00"),
            pd.Timestamp("2026-08-28 13:00")]

    def fake_end(schema):
        asked.append(schema)
        return ends[min(len(asked) - 1, len(ends) - 1)]

    monkeypatch.setattr(db_live, "available_end", fake_end)
    monkeypatch.setattr(db_nautilus, "FRONTIER_FLOOR", 0)
    monkeypatch.setattr(db_nautilus, "FRONTIER_EVERY", 0)

    client = types.SimpleNamespace(_log=_Recorder())
    how = asyncio.run(db_nautilus.DatabentoLiveClient._wait_for_frontier(
        client, "1h", pd.Timestamp("2026-08-28 13:00")))
    assert how == "arrived"
    assert asked == ["ohlcv-1h"] * 3, "it must keep asking until the bar is in the archive"


def test_an_unreadable_frontier_degrades_to_the_fixed_lag(monkeypatch):
    """No key, an HTTP error, a vendor schema change: the poll task must survive all three.

    A raise here would kill the task and take the leg's feed with it, silently — the same
    shape of failure `run_paper` builds a controller for. It falls back to what this desk
    did before the frontier was consulted.
    """
    def boom(schema):
        raise RuntimeError("no key")

    monkeypatch.setattr(db_live, "available_end", boom)
    monkeypatch.setattr(db_nautilus, "FRONTIER_FLOOR", 0)
    monkeypatch.setattr(db_nautilus, "POLL_LAG", 0)
    monkeypatch.setattr(db_nautilus, "POLL_LAG_BY_TF", {})

    client = types.SimpleNamespace(_log=_Recorder())
    how = asyncio.run(db_nautilus.DatabentoLiveClient._wait_for_frontier(
        client, "1h", pd.Timestamp("2026-08-28 13:00")))
    assert how == "unreadable"
    assert any("frontier" in w for w in client._log.warnings)


async def _noop():
    return None


class _Recorder:
    def __init__(self):
        self.errors, self.warnings, self.infos = [], [], []

    def error(self, msg):
        self.errors.append(str(msg))

    def warning(self, msg):
        self.warnings.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))


def _subscribe(bar_type, usable=True, stream=None):
    """Drive the real `_subscribe_bars` against a stand-in client.

    Unbound, on a plain object, because constructing a `LiveMarketDataClient` needs a
    loop, a message bus and a cache — none of which this property depends on. What is
    under test is the ORDER of the guards: the refusal has to happen before a poll task
    exists, or the failure lands inside a Nautilus task where it is logged and goes
    nowhere.
    """
    client = types.SimpleNamespace(_poll_tasks={}, _usable=usable, _log=_Recorder(),
                                   _stream=stream, _streamed={},
                                   create_task=lambda coro: coro.close())
    # A stand-in for the poll coroutine: what is under test is whether a task is created
    # at all, never what it then does against the vendor.
    client._poll = lambda bar_type: _noop()
    command = types.SimpleNamespace(bar_type=bar_type)
    asyncio.run(db_nautilus.DatabentoLiveClient._subscribe_bars(client, command))
    return client


def test_subscribing_an_unservable_timeframe_raises_at_subscribe_time():
    """Loudly, and before a task is created — not quietly, inside one, forever."""
    with pytest.raises(ValueError):
        _subscribe(BarType.from_str("ES.v.0.GLBX-4-HOUR-LAST-EXTERNAL"))


def test_with_no_key_the_subscription_is_refused_rather_than_left_waiting():
    """The state the VPS is in until somebody provisions `DATABENTO_API_KEY`.

    A subscription that starts and never delivers is worse than one that is refused: the
    strategy reads `live` and every order it sends is rejected for want of a price that
    was never coming. So no poll task is created, and the reason is in the log.
    """
    client = _subscribe(BarType.from_str("ES.v.0.GLBX-1-DAY-LAST-EXTERNAL"), usable=False)
    assert client._poll_tasks == {}, "a task was started with no credential behind it"
    assert any(db_live.NO_KEY in m for m in client._log.errors)


def test_a_healthy_subscription_does_start_a_poll_task():
    """The negative tests above are only meaningful if the positive one passes."""
    client = _subscribe(BarType.from_str("ES.v.0.GLBX-1-DAY-LAST-EXTERNAL"), usable=True)
    assert list(client._poll_tasks) == [
        BarType.from_str("ES.v.0.GLBX-1-DAY-LAST-EXTERNAL")]


def test_a_missing_key_is_answered_not_raised():
    """`db_loader.api_key` raises; the desk holds live positions on four other classes
    and runs under systemd with a restart policy, so `have_key` must answer instead."""
    import db_loader
    original = db_loader.api_key
    db_loader.api_key = lambda: (_ for _ in ()).throw(RuntimeError("no key"))
    try:
        assert db_live.have_key() is False
    finally:
        db_loader.api_key = original


def test_the_desk_refuses_a_futures_registration_it_cannot_feed():
    """`desk_control._feedable` is the check that reaches the OWNER of a registration.

    The client's refusal above is the binding one; this one is the readable one — it lands
    on the row as a `reason` instead of in a task's log.
    """
    import desk_control
    ok, why = desk_control._feedable(FUTURES, "1d")
    assert ok and why == ""
    ok, why = desk_control._feedable(FUTURES, "4h")
    assert not ok and "4h" in why
    ok, why = desk_control._feedable("us_stocks", "4h")
    assert ok, "every other class is fed by Twelve Data and is not this check's business"


def test_a_missing_sheet_degrades_instead_of_exiting():
    """`FORWARD_TIMEFRAMES` offers 4h and this class has no 4h sheet, permanently.
    `top_rules` raises SystemExit on a missing one, which is right for a research script
    and would take the desk down here."""
    assert paper_config.has_sheet(FUTURES, "1d")
    assert not paper_config.has_sheet(FUTURES, "4h")
    with pytest.raises(SystemExit):
        paper_config.top_rules(FUTURES, 3, "4h")


# ------------------------------------------------------------------ the roll

def _synthetic_roll():
    """Ten daily bars across one roll, with rank 1 visible on the same bars as rank 0.

    The incoming contract trades 4% above the outgoing one — a wide roll, chosen so that
    an unadjusted stitch would be unmistakable rather than plausible.
    """
    index = pd.date_range("2026-01-01", periods=10, freq="D", name="Date")
    close = np.array([100.0, 101.0, 100.5, 102.0, 101.5, 103.0,
                      107.12, 108.0, 107.0, 109.0])
    ids = np.array([111] * 6 + [222] * 4, dtype="int64")
    front = pd.DataFrame({"Open": close * 0.999, "High": close * 1.004,
                          "Low": close * 0.996, "Close": close,
                          "Volume": np.full(10, 1000.0), "instrument_id": ids},
                         index=index)
    # Rank 1 IS the contract rank 0 rolls into, priced on every bar: 4% above the front
    # while the front is 111, and (unused) something else afterwards.
    behind_close = np.where(ids == 111, close * 1.04, close * 1.04)
    behind = pd.DataFrame({"Open": behind_close, "High": behind_close,
                           "Low": behind_close, "Close": behind_close,
                           "Volume": np.full(10, 10.0),
                           "instrument_id": np.array([222] * 10, dtype="int64")},
                          index=index)
    return front, behind


def test_the_roll_ratio_is_read_from_the_same_bar_and_is_exact():
    ratios = db_live.roll_ratios(*_synthetic_roll(), "XX.v.0")
    assert list(ratios) == [(111, 222)]
    ratio, method = ratios[(111, 222)]
    assert method == "same-bar rank 1", (
        "the close-to-close splice folds a session of market movement into the adjustment")
    assert ratio == pytest.approx(1.04, abs=1e-9)


def test_the_forward_factor_divides_where_back_adjust_multiplies():
    """The direction, which is the one thing easy to get backwards.

    `back_adjust` scales HISTORY up to the newest contract. The live buffer's history is
    already published and cannot be rewritten, so it is the new bars that come down to the
    anchor the warm-up chose. Same adjustment, opposite end.
    """
    front, behind = _synthetic_roll()
    client = types.SimpleNamespace(_factor={"XX.v.0": 1.0}, _contract={"XX.v.0": 111},
                                   _log=_Recorder())
    client._publish_factor = types.MethodType(
        db_nautilus.DatabentoLiveClient._publish_factor, client)
    db_nautilus.DatabentoLiveClient._apply_roll(client, "XX.v.0", front, behind, 222)
    assert client._factor["XX.v.0"] == pytest.approx(1.0 / 1.04, abs=1e-12)
    assert client._contract["XX.v.0"] == 222
    assert db_live.FORWARD_FACTORS["XX.v.0"] == pytest.approx(1.0 / 1.04, abs=1e-12)
    assert not client._log.warnings, "an exact roll must not warn"


def test_the_live_buffer_reproduces_back_adjust_up_to_one_constant():
    """The whole claim, end to end.

    Warm up on the six bars before the roll — adjusted by `db_loader.back_adjust`, which
    over a window with no roll in it is the identity — then carry the forward factor
    across the roll and scale the four bars after it. Compare against `back_adjust` over
    the WHOLE window, which is what the cache holds.

    The two are NOT equal, and must not be expected to be: they are anchored on different
    contracts. They are equal up to a constant, which is exactly what a price indicator in
    this repo can see, because every one of them is equivariant under a common scale. So
    the assertion is on the SPREAD of the ratio, not on its level.
    """
    import db_loader

    front, behind = _synthetic_roll()
    cached, ledger = db_loader.back_adjust(front, behind, "XX.v.0")
    assert len(ledger) == 1

    warm, _ = db_loader.back_adjust(front.iloc[:6], behind.iloc[:6], "XX.v.0")
    factor = 1.0 / db_live.roll_ratios(front, behind, "XX.v.0")[(111, 222)][0]
    live = front.iloc[6:].drop(columns=["instrument_id"]).copy()
    for col in ("Open", "High", "Low", "Close"):
        live[col] = live[col] * factor
    stitched = pd.concat([warm, live])

    assert list(stitched.index) == list(cached.index)
    ratio = stitched["Close"] / cached["Close"]
    assert ratio.max() / ratio.min() == pytest.approx(1.0, abs=1e-12), (
        "a jump in this ratio is a roll the live path handled differently from the cache")
    # And the unadjusted series is unmistakably NOT that, which is what the whole
    # machinery is for: the raw stitch carries a +4% return nobody earned.
    raw = front["Close"] / cached["Close"]
    assert raw.max() / raw.min() > 1.03


def test_an_unmeasurable_roll_falls_back_loudly_and_still_adjusts():
    """Never an unadjusted bar across a roll, silently or otherwise.

    When rank 1 was not the contract rank 0 became — ranks can skip a month — the exact
    ratio is unavailable and the ordinary splice is used. It is labelled in
    `db_loader`'s ledger and it is a WARNING here, because on this leg the number it
    distorts is a live position's entry.
    """
    front, behind = _synthetic_roll()
    behind["instrument_id"] = 999                  # rank 1 is somebody else entirely
    client = types.SimpleNamespace(_factor={"XX.v.0": 1.0}, _contract={"XX.v.0": 111},
                                   _log=_Recorder())
    client._publish_factor = types.MethodType(
        db_nautilus.DatabentoLiveClient._publish_factor, client)
    db_nautilus.DatabentoLiveClient._apply_roll(client, "XX.v.0", front, behind, 222)
    assert client._log.warnings, "an inexact adjustment must say so"
    assert client._factor["XX.v.0"] < 1.0, "the bar was adjusted, not passed through raw"


# ------------------------------------------------------------------ the live gateway
#
# `db_stream` is the LIVE feed, and everything below is offline: records are handed to
# `FuturesStream._record` directly, exactly as the SDK's reader thread would. Nothing here
# opens a socket — the vendor path is exercised by `python db_stream.py`, which streams
# real roots and then checks the same minutes against the REST archive, and cannot be a
# unit test for the reason `db_live._smoke` cannot.


def _rec(kind: str, **fields):
    """A duck-typed DBN record.

    `db_stream._record` dispatches on `type(rec).__name__` rather than on `isinstance`,
    and that is what makes this possible: the SDK's record classes are Rust extension
    types with no public constructor, so an `isinstance` dispatch would force every test
    of the decoding to open a live session.
    """
    rec = type(kind, (), {})()
    for name, value in fields.items():
        setattr(rec, name, value)
    return rec


def _map(symbol: str, iid: int):
    return _rec("SymbolMappingMsg", stype_in_symbol=symbol, instrument_id=iid)


def _ohlcv(iid: int, ts, o, h, l, c, volume=1000.0, timeframe="1m"):
    """One completed interval, with prices in DBN's 1e-9 fixed point as the wire has them."""
    rtype = {"1m": 33, "1h": 34, "1d": 35}[timeframe]
    return _rec("OHLCVMsg", instrument_id=iid, rtype=rtype,
                ts_event=int(pd.Timestamp(ts).value),
                open=int(round(o * db_stream.PRICE_SCALE)),
                high=int(round(h * db_stream.PRICE_SCALE)),
                low=int(round(l * db_stream.PRICE_SCALE)),
                close=int(round(c * db_stream.PRICE_SCALE)),
                volume=volume)


def _end_of_interval():
    return _rec("SystemMsg", msg="End of interval for ohlcv-1m")


class _Stream:
    """A `FuturesStream` with no session behind it, collecting what it would publish."""

    def __init__(self, symbol="XX.v.0", timeframe="1m"):
        self.published: list[tuple] = []
        self.modes: list[tuple[str, str]] = []
        self.log = _Recorder()
        self.stream = db_stream.FuturesStream(
            lambda s, tf, front, behind: self.published.append((s, tf, front, behind)),
            lambda mode, why: self.modes.append((mode, why)), self.log)
        self.stream.subscribe(symbol, timeframe)

    def feed(self, *records):
        for rec in records:
            self.stream._record(rec)


def test_a_streamed_price_is_decoded_off_the_wire_scale():
    """DBN carries a price as an integer of 1e-9 units and the REST path never sees one.

    `db_loader._get_range` passes `pretty_px=true`, so the archive's CSV already holds
    decimals; a record off the socket does not. Verified against the vendor on 2026-08-28:
    `close=7740500000000` is ES at 7740.50, matching the archive's bar for the same
    minute. Getting this wrong by a factor of a billion would not be subtle, but getting
    it wrong by reading `pretty_close` — a convenience accessor, not the wire format —
    would be silent the day the SDK changed what it returns.
    """
    s = _Stream()
    s.feed(_map("XX.v.0", 111),
           _ohlcv(111, "2026-01-01 00:00", 7740.25, 7741.0, 7740.0, 7740.5),
           _end_of_interval())
    assert len(s.published) == 1
    front = s.published[0][2]
    assert float(front["Close"].iloc[-1]) == pytest.approx(7740.5, abs=1e-9)
    assert float(front["High"].iloc[-1]) == pytest.approx(7741.0, abs=1e-9)


def test_a_streamed_frame_is_the_shape_back_adjust_takes():
    """The stream's whole output contract, asserted as a shape.

    `db_live.fetch_raw` returns `Open/High/Low/Close/Volume/instrument_id` on a tz-naive
    UTC index, and `db_loader.back_adjust` reads `instrument_id` as an int64 column. If
    the streamed frame differs in any of that, the roll arithmetic silently takes a
    different branch — the `instrument_id` check falls through to the close-to-close
    splice — rather than failing.
    """
    s = _Stream()
    s.feed(_map("XX.v.0", 111),
           _ohlcv(111, "2026-01-01 00:00", 100.0, 101.0, 99.0, 100.5),
           _end_of_interval())
    front = s.published[0][2]
    assert list(front.columns) == ["Open", "High", "Low", "Close", "Volume",
                                   "instrument_id"]
    assert front["instrument_id"].dtype == np.int64
    assert front["Volume"].dtype == np.float64
    assert front.index.tz is None and front.index.name == "Date"
    # And `back_adjust` accepts it, which is the only claim that actually matters.
    bars, ledger = __import__("db_loader").back_adjust(front, front, "XX.v.0")
    assert list(bars.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert ledger.empty


def test_a_bar_for_an_unmapped_contract_is_dropped_rather_than_misfiled():
    """Symbology arrives as its own message, and a bar can beat it on a reconnect.

    Filing it under a guess would put another root's price into this root's buffer, which
    is the `CTRA -> Ciputra` failure of the root `CLAUDE.md` with an integer instead of a
    ticker. Dropping it costs one bar; the next mapping message repairs the rest.
    """
    s = _Stream()
    s.feed(_ohlcv(999, "2026-01-01 00:00", 100.0, 101.0, 99.0, 100.5),
           _end_of_interval())
    assert s.published == []


def test_at_a_roll_rank_zero_wins_the_contract_both_ranks_briefly_name():
    """The one symbology race a continuous stream has.

    A roll is the gateway re-pointing `XX.v.0` at the contract `XX.v.1` was already
    naming, and the two mapping messages do not arrive at the same instant. In between,
    both ranks name instrument 222. Filing that bar under whichever message came last
    would put the FRONT's price into the rank-1 buffer, and `back_adjust` would then read
    a roll ratio of 1.0 off it — an unadjusted roll, arrived at through a coincidence,
    with nothing in the log. Rank 0 wins, and rank 1 simply has no bar for that interval,
    which is correct: the ratio is read off the bar BEFORE the roll, where the two were
    still different contracts.
    """
    s = _Stream()
    s.feed(_map("XX.v.0", 111), _map("XX.v.1", 222))
    assert s.stream._symbol_of == {111: "XX.v.0", 222: "XX.v.1"}
    s.feed(_map("XX.v.0", 222))               # the roll, rank 1's remap not yet arrived
    assert s.stream._symbol_of[222] == "XX.v.0", "the front must win the contested id"
    s.feed(_ohlcv(222, "2026-01-01 00:05", 104.0, 104.0, 104.0, 104.0),
           _end_of_interval())
    symbol, _, front, behind = s.published[-1]
    assert symbol == "XX.v.0"
    assert int(front["instrument_id"].iloc[-1]) == 222
    assert 222 not in set(behind["instrument_id"]), (
        "the front's own bar must not also be filed as rank 1, or the roll prices at 1.0")


def _stream_the_synthetic_roll():
    """Push `_synthetic_roll`'s ten bars through the stream, one interval at a time."""
    front, behind = _synthetic_roll()
    s = _Stream()
    s.feed(_map("XX.v.0", int(front["instrument_id"].iloc[0])),
           _map("XX.v.1", int(behind["instrument_id"].iloc[0])))
    for ts in front.index:
        fid = int(front.loc[ts, "instrument_id"])
        if fid != s.stream._iid_of["XX.v.0"]:
            s.feed(_map("XX.v.0", fid))       # the roll, exactly as the gateway sends it
        s.feed(_ohlcv(fid, ts, *[float(front.loc[ts, c])
                                 for c in ("Open", "High", "Low", "Close")]))
        bid = int(behind.loc[ts, "instrument_id"])
        if bid != fid:
            s.feed(_ohlcv(bid, ts, *[float(behind.loc[ts, c])
                                     for c in ("Open", "High", "Low", "Close")]))
        s.feed(_end_of_interval())
    return s, front, behind


def test_the_streamed_roll_reproduces_back_adjust_up_to_one_constant():
    """**The hard constraint, on the streamed path.**

    The live gateway delivers RAW continuous prices, exactly as the poller's raw fetch
    does, so a roll that reached a strategy unadjusted would hand it a return nobody
    earned — WTI's was +37% in April 2020. This drives the REAL `_emit`, the poller's
    publish path, over frames the REAL `FuturesStream` built out of gateway records, and
    compares the result against `db_loader.back_adjust` over the whole window, which is
    what the cache holds.

    As in `test_the_live_buffer_reproduces_back_adjust_up_to_one_constant`, the two are
    equal up to a CONSTANT and not equal: they are anchored on different contracts, and a
    constant scale is exactly what a price indicator in this repo cannot see. So the
    assertion is on the spread of the ratio.
    """
    import db_loader

    s, raw_front, raw_behind = _stream_the_synthetic_roll()
    cached, ledger = db_loader.back_adjust(raw_front, raw_behind, "XX.v.0")
    assert len(ledger) == 1, "the fixture must contain exactly one roll"

    bar_type = BarType.from_str("XX.v.0.GLBX-1-MINUTE-LAST-EXTERNAL")
    emitted: dict[pd.Timestamp, dict] = {}
    client = types.SimpleNamespace(
        _factor={}, _contract={}, _last_open={}, _log=_Recorder(),
        _to_bar=lambda bt, ts, row: emitted.setdefault(ts, dict(row)),
        # `_to_bar` returns the scaled row and `_handle_data` is where a real client hands
        # it to the message bus. Stubbing the bus rather than building one keeps the test
        # on the arithmetic, which is the property under test.
        _handle_data=lambda bar: None)
    for name in ("_publish_factor", "_apply_roll", "_emit"):
        setattr(client, name, types.MethodType(
            getattr(db_nautilus.DatabentoLiveClient, name), client))
    # The anchor the warm-up would have set: bars up to the roll, on the first contract.
    client._contract["XX.v.0"] = int(raw_front["instrument_id"].iloc[0])
    client._publish_factor("XX.v.0", 1.0)

    for _, _, front, behind in s.published:
        newest = client._emit(bar_type, front, behind)
        if newest is not None:
            client._last_open[bar_type] = newest

    assert set(emitted) == set(raw_front.index), "every streamed bar must be published"
    # The EXACT branch, not the splice. `_synthetic_roll` is built so both give 1.04, so
    # the scale check below cannot tell them apart — and a stream silently falling back to
    # a close-to-close splice on every roll is precisely the regression worth catching:
    # a splice folds a session of market movement into a live position's entry.
    assert not client._log.warnings, (
        "the same-bar rank-1 ratio must be measurable from the streamed buffers")
    assert any("same-bar rank 1" in m for m in client._log.infos)
    live = pd.Series({ts: emitted[ts]["Close"] for ts in emitted}).sort_index()
    ratio = live / cached["Close"]
    assert ratio.max() / ratio.min() == pytest.approx(1.0, abs=1e-12), (
        "a jump here is a roll the streamed path handled differently from the cache")
    # And the raw stream is unmistakably NOT that, which is what the machinery is for.
    raw = raw_front["Close"] / cached["Close"]
    assert raw.max() / raw.min() > 1.03


def test_a_dropped_session_falls_back_to_the_poller_and_says_which_mode_it_is_in():
    """A silent downgrade to an eight-minute feed is worse than either mode deliberately.

    Three things have to happen together and none of them is the others: the leg must not
    go dark, `db_live.FEED_MODE` must say `poll`, and every subscription the stream was
    carrying must get a poll task — not just the ones a strategy re-subscribes later.
    """
    before = dict(db_live.FEED_MODE)
    try:
        started: list = []
        client = types.SimpleNamespace(
            _poll_tasks={}, _log=_Recorder(),
            _streamed={("ES.v.0", "1m"): BarType.from_str(
                "ES.v.0.GLBX-1-MINUTE-LAST-EXTERNAL")},
            create_task=lambda coro: (coro.close(), started.append(1))[0])
        client._poll = lambda bar_type: _noop()
        for name in ("_set_mode", "_fall_back_to_polling"):
            setattr(client, name, types.MethodType(
                getattr(db_nautilus.DatabentoLiveClient, name), client))

        client._fall_back_to_polling()
        client._set_mode("poll", "the gateway did not come back")

        assert list(client._poll_tasks) == [
            BarType.from_str("ES.v.0.GLBX-1-MINUTE-LAST-EXTERNAL")]
        assert client._streamed == {}, "a bar type must not be both streamed and polled"
        assert db_live.feed_mode() == "poll"
        assert "did not come back" in db_live.FEED_MODE["why"]
        assert any("minutes after they close" in m for m in client._log.warnings), (
            "the downgrade has to name what it costs, not just that it happened")
    finally:
        db_live.FEED_MODE.clear()
        db_live.FEED_MODE.update(before)


def test_the_stream_degrades_rather_than_raising_when_there_is_nothing_to_stream_with():
    """`have_sdk` and `have_key` are answered, never raised — `db_live.have_key`'s reason.

    The VPS is the live example of both: `databento` was never a dependency of this repo,
    so a box that has not been re-provisioned has `db_loader` working over `requests` and
    no SDK at all. That must degrade the futures leg, never restart-loop a desk holding
    live positions on four other classes.
    """
    modes: list[tuple[str, str]] = []
    log = _Recorder()
    stream = db_stream.FuturesStream(lambda *a: None,
                                     lambda mode, why: modes.append((mode, why)), log)
    original = db_stream.have_sdk
    db_stream.have_sdk = lambda: False
    try:
        assert stream.start() is False
        assert stream.degraded and stream.mode() == "poll"
        assert modes and modes[-1][0] == "poll" and "databento" in modes[-1][1]
    finally:
        db_stream.have_sdk = original


def test_a_reconnect_replays_the_gap_rather_than_resuming_live():
    """The whole difficulty of a stream, and the reason the SDK's own policy is not used.

    `databento.live.session._reconnect` re-subscribes with `start=None`, so its reconnect
    resumes live and the outage's bars are simply gone. `db_stream` owns the reconnect
    instead, and the first rung of its ladder asks the gateway to replay from the last bar
    it saw — which is what makes the buffer hole-free rather than merely continuous.
    """
    log = _Recorder()
    stream = db_stream.FuturesStream(lambda *a: None, lambda *a: None, log)
    left_off = pd.Timestamp("2026-01-01 00:00")
    stream._last_bar["1m"] = left_off
    rungs = stream._replay_starts("1m")
    assert rungs[0] == left_off.tz_localize("UTC"), (
        "the first attempt must recover the whole gap, not a fixed window")
    assert rungs[-1] is None, "live-only has to remain reachable, or an outage is fatal"


def test_the_replay_ladder_falls_back_and_says_what_it_lost():
    """A gateway that refuses a replay window must cost a warning, never the session.

    Intraday replay reaches back to the start of the current session and no further, so a
    desk that was down overnight gets rung 1 refused. Rung 2 still recovers the last
    quarter of an hour, which is what closes the gap between a REST warm-up — stale by
    `ARCHIVE_LAG_SECONDS` by construction — and a socket that would otherwise begin now.
    """
    log = _Recorder()
    stream = db_stream.FuturesStream(lambda *a: None, lambda *a: None, log)
    stream._last_bar["1m"] = pd.Timestamp("2020-01-01 00:00")   # far outside the window
    stream.subscribe("ES.v.0", "1m")
    seen: list = []

    class _Gateway:
        def subscribe(self, **kw):
            seen.append(kw.get("start"))
            if len(seen) == 1:
                raise ValueError("replay start is before the session began")
            return len(seen)

    stream._send(_Gateway(), sorted(stream._wanted), catchup=True)
    assert len(seen) == 2, "the ladder must try again rather than give the session up"
    assert seen[0] is not None and seen[1] is not None
    assert seen[1] > seen[0], "rung 2 is a SHORTER window, not no window"
    assert any("refused a replay" in m for m in log.warnings)
    # Both ranks, in one request, for the same reason `fetch_raw` asks for both.
    assert stream._symbols_for(("ES.v.0", "1m")) == ["ES.v.0", "ES.v.1"]


def test_a_late_subscription_rebuilds_the_session_rather_than_taking_a_hole():
    """`Live.subscribe` cannot carry a `start` once `Live.start()` has run.

    So a root registered after the desk came up would begin at "now", leaving a hole
    between its REST warm-up — ~8 minutes stale by construction — and its first live bar.
    A new session is the only way to replay it, and the burst at start-up is why that is
    debounced rather than done per subscription.
    """
    log = _Recorder()
    stream = db_stream.FuturesStream(lambda *a: None, lambda *a: None, log)
    stream.subscribe("ES.v.0", "1m")
    stream._client = object()                  # pretend a session is already carrying
    stream._live_set = {("ES.v.0", "1m")}
    stream._last_record = time.monotonic()     # ...and that it is healthy, not silent
    stream.subscribe("CL.v.0", "1m")

    opened: list = []
    stream._open = lambda: (opened.append(sorted(stream._wanted)), True)[1]
    stream._sweep()
    assert opened == [], "a rebuild inside the debounce would be one socket per strategy"
    stream._wanted_changed_at -= db_stream.SUBSCRIBE_DEBOUNCE_SECONDS + 1
    stream._sweep()
    assert opened == [[("CL.v.0", "1m"), ("ES.v.0", "1m")]], (
        "the rebuild must carry everything, not only the new subscription")


def test_a_rebuild_gives_up_its_overlap_when_the_connection_cap_refuses_it():
    """Measured against the real gateway on 2026-08-28, not imagined.

    A rebuild deliberately holds the old socket until the new one is carrying, so the
    changeover costs a duplicate bar rather than a hole — `_emit` can filter a bar it has
    already published and cannot recover one that never arrived. Databento caps concurrent
    live sessions per key, though, so that overlap is exactly what the cap refuses. When
    the refusal names the cap, the ordering is inverted for one attempt: a leg with no
    session at all is worse than a leg missing a bar.
    """
    log = _Recorder()
    stream = db_stream.FuturesStream(lambda *a: None, lambda *a: None, log)
    stream.subscribe("ES.v.0", "1m")
    killed: list = []
    stream._client = types.SimpleNamespace(terminate=lambda: killed.append(1))

    attempts: list = []

    def fake_live(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise RuntimeError("BentoError: User has reached their "
                               + db_stream.CONNECTION_LIMIT)
        return types.SimpleNamespace(add_callback=lambda *a: None,
                                     subscribe=lambda **k: 1,
                                     start=lambda: None,
                                     terminate=lambda: None)

    import databento
    original = databento.Live
    databento.Live = fake_live
    try:
        assert stream._open() is True, "the retry must succeed, not give the session up"
    finally:
        databento.Live = original
    assert killed, "the old socket has to be dropped before the retry, or the cap refuses"
    assert any(db_stream.CONNECTION_LIMIT in m or "connection cap" in m
               for m in log.warnings), "a hole taken deliberately must be said out loud"


def test_only_the_sizes_that_gain_from_a_socket_are_streamed():
    """`1d` is fed by the poller on purpose, and the reason is not latency.

    `db_loader.merge_session_stubs` folds Sunday's two-hour opening sliver into the
    session it opens — or DROPS it when the weekend carried the roll, which 41% of them
    do — and it decides that by looking at the NEXT session's contract. A stream has no
    next bar yet, so it would publish a Sunday stub as a day, and `IBS` is `(C-L)/(H-L)`:
    it reads exactly that geometry.
    """
    assert db_stream.can_stream("1m") and db_stream.can_stream("1h")
    assert not db_stream.can_stream("1d"), "a daily bar has a batch step after it"
    assert not db_stream.can_stream("4h"), "the archive has no such schema at all"
    assert db_live.can_feed("1d"), "...and yet 1d is perfectly feedable, by the poller"


def test_a_daily_subscription_polls_even_with_a_healthy_stream():
    """The routing above, asserted where it actually binds — at subscribe time."""
    stream = types.SimpleNamespace(
        subscribe=lambda symbol, tf: db_stream.can_stream(tf))
    daily = _subscribe(BarType.from_str("ES.v.0.GLBX-1-DAY-LAST-EXTERNAL"), stream=stream)
    assert list(daily._poll_tasks) and daily._streamed == {}
    minute = _subscribe(BarType.from_str("ES.v.0.GLBX-1-MINUTE-LAST-EXTERNAL"),
                        stream=stream)
    assert minute._poll_tasks == {}, "a streamed bar type must not also be polled"
    assert minute._streamed == {("ES.v.0", "1m"):
                                BarType.from_str("ES.v.0.GLBX-1-MINUTE-LAST-EXTERNAL")}


def test_warm_up_cannot_wind_the_watermark_back_over_a_streamed_bar():
    """A race the poller never had, because its first fetch was fifteen minutes away.

    Warm-up comes from the REST archive and is ~8 minutes stale by construction; a
    streamed bar can land within seconds of the subscription. So `_request_bars` can
    complete after a bar has already been published, and assigning `_last_open` there
    would wind the watermark back and re-emit every bar in between.
    """
    bar_type = BarType.from_str("ES.v.0.GLBX-1-MINUTE-LAST-EXTERNAL")
    client = types.SimpleNamespace(_last_open={bar_type: pd.Timestamp("2026-01-01 00:09")})
    warm = pd.Timestamp("2026-01-01 00:01")
    seen = client._last_open.get(bar_type)
    client._last_open[bar_type] = warm if seen is None else max(seen, warm)
    assert client._last_open[bar_type] == pd.Timestamp("2026-01-01 00:09")


def test_the_feed_can_be_chosen_deliberately():
    """A switch, because a deliberate choice of the slower feed is a legitimate thing to
    want — comparing the two, or working around a vendor incident — and because a typo in
    a systemd unit must not be the thing that stops a desk starting."""
    assert db_stream.wanted_mode({}) == "stream"
    assert db_stream.wanted_mode({db_stream.FEED_ENV: "poll"}) == "poll"
    assert db_stream.wanted_mode({db_stream.FEED_ENV: "POLL "}) == "poll"
    assert db_stream.wanted_mode({db_stream.FEED_ENV: "typo"}) == "stream"


def test_the_caveat_tracks_the_feed_instead_of_asserting_a_constant():
    """`desk_control._caveat` told every 1m futures member their fills were priced off a
    bar eight minutes old. On the live gateway that is untrue, and a caveat that is untrue
    is worse than none — keep the column rare or nobody reads it. On the poller it is
    still true, and the fallback can happen at any moment, so it is read at the moment the
    registration is marked rather than written once."""
    import desk_control
    before = dict(db_live.FEED_MODE)
    try:
        db_live.FEED_MODE.update(mode="stream", why="live gateway")
        assert desk_control._feed_caveat("cme_futures", "1m") == ""
        db_live.FEED_MODE.update(mode="poll", why="the gateway did not come back")
        caveat = desk_control._feed_caveat("cme_futures", "1m")
        assert "HISTORICAL archive" in caveat
        assert "the gateway did not come back" in caveat, (
            "a member told their fills are stale must be told WHY, or it reads as design")
        assert desk_control._feed_caveat("cme_futures", "1d") == ""
        assert desk_control._feed_caveat("us_stocks", "1m") == ""
    finally:
        db_live.FEED_MODE.clear()
        db_live.FEED_MODE.update(before)


# ------------------------------------------------------------------ the other vendor

def test_twelve_data_is_never_asked_about_a_futures_symbol():
    """`td_live.fetch_prices` batches every marked symbol into one call, and Twelve Data
    answers an unqualified `ES` with Eversource Energy rather than with an error."""
    import run_paper
    marked = ["AAPL", "BTC/USD", "ES.v.0", "XAU/USD", "GC.v.0"]
    td, futures = run_paper._split_by_feed(marked)
    assert futures == ["ES.v.0", "GC.v.0"]
    assert "ES.v.0" not in td and "GC.v.0" not in td
    assert td == ["AAPL", "BTC/USD", "XAU/USD"]


def test_an_unknown_symbol_does_not_stop_the_marker():
    """`class_of` raises SystemExit on one; a reporting thread must not be able to."""
    import run_paper
    td, futures = run_paper._split_by_feed(["NOT-A-DESK-SYMBOL"])
    assert td == ["NOT-A-DESK-SYMBOL"] and futures == []


def test_alpaca_still_refuses_the_class():
    """Alpaca sells no futures, and it says so by name rather than being merely absent."""
    import alpaca_client
    assert FUTURES in alpaca_client.UNSUPPORTED
    assert FUTURES not in alpaca_client.CLASS_ENV
    with pytest.raises(KeyError):
        alpaca_client.credentials(FUTURES)
    assert FUTURES not in alpaca_client.configured_classes()
