r"""The CME futures leg: the instrument, the feed's refusals, and the roll arithmetic.

Run from THIS directory::

    ..\.venv\Scripts\python -m pytest test_futures_leg.py -q

Offline. Nothing here reaches Databento — the vendor path is exercised by
`python db_live.py`, which is a smoke run against the real archive and cannot be a unit
test, because a test that fails when a vendor is slow is a test nobody will trust.

Four properties, and each one is a failure this folder has already paid for once wearing a
different hat:

* **The instrument is fractional.** `FuturesContract` has no `size_increment`, so a whole
  contract against a $6,250 slice rounds ES to zero and the whole book sits flat while
  every log line reads healthy. Same shape as the crypto one-increment fills.
* **The legs stay disjoint.** `class_of` is a reverse lookup and decides the sheet, the
  venue and the feed. A symbol in two legs resolves to whichever was declared first.
* **An unfeedable timeframe is refused AT SUBSCRIBE TIME.** Present is not feedable: the
  GLBX archive has no 4h or 15m schema at all. A bar type that can be spelled and never
  subscribed to is the fifteen-hour silent failure `test_feed_timeframes.py` is named for.
* **The live roll reproduces the cache's back-adjustment.** Not to the same anchor — the
  live buffer is anchored where its warm-up landed and the cache at its newest bar — but
  to the same series up to one global constant, which is all a scale-equivariant indicator
  can tell apart.
"""

from __future__ import annotations

import asyncio
import types

import numpy as np
import pandas as pd
import pytest

import paper_config
import db_live
import db_nautilus
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

    $100,000 across sixteen names is ~$6,250 a slice. One REAL E-mini S&P contract is 50
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

@pytest.mark.parametrize("tf", ["1d", "1h"])
def test_the_two_clean_schemas_round_trip(tf):
    spec = paper_config.BAR_SPEC[tf]
    assert db_nautilus.timeframe_of(
        BarType.from_str(f"ES.v.0.GLBX-{spec}")) == tf


@pytest.mark.parametrize("spec", ["4-HOUR-LAST-EXTERNAL", "15-MINUTE-LAST-EXTERNAL",
                                  "5-MINUTE-LAST-EXTERNAL", "1-MINUTE-LAST-EXTERNAL"])
def test_an_unservable_timeframe_is_refused(spec):
    """The GLBX ohlcv archive has no 4h or 15m schema, and its 1m bars fold whole
    sessions before 2016. The sheets at those sizes were cut from cached files, which a
    live poll cannot ask for."""
    with pytest.raises(ValueError):
        db_nautilus.timeframe_of(BarType.from_str(f"ES.v.0.GLBX-{spec}"))


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


def _subscribe(bar_type, usable=True):
    """Drive the real `_subscribe_bars` against a stand-in client.

    Unbound, on a plain object, because constructing a `LiveMarketDataClient` needs a
    loop, a message bus and a cache — none of which this property depends on. What is
    under test is the ORDER of the guards: the refusal has to happen before a poll task
    exists, or the failure lands inside a Nautilus task where it is logged and goes
    nowhere.
    """
    client = types.SimpleNamespace(_poll_tasks={}, _usable=usable, _log=_Recorder(),
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
