"""Twelve Data as a Nautilus live data client, plus the instruments to trade against.

This is the piece that makes `backtest -> paper -> live` one code path instead of three
codebases. Nautilus already runs the backtest (`../backtest engine/engines/nautilus.py`);
here the same engine is fed live bars from the same vendor the research used, and paired
with `SandboxExecutionClient` — real prices in, simulated fills out. Going live later
swaps the execution client and touches nothing else.

Design notes worth keeping:

**Bars, not ticks, and that is a parity decision rather than a convenience.**
`SandboxExecutionClient(bar_execution=True)` fills from the bar that produced the signal,
so every fill lands at that bar's close — the same price the backtest assumed. There IS a
live tick stream now (`live_ws.LiveHub`), and it deliberately does not feed this client:
bars aggregated from ticks would not be the `/time_series` bars the research cache was
built from, and the forward record would stop being comparable to the sheet with nothing
to indicate it. The stream marks positions and reports feed health; it does not make bars.

**One poll task per subscription, aligned to the close.** The task sleeps until the next
bar boundary plus the timeframe's poll lag, then fetches. The lag exists because a vendor
stamps a bar at its open and needs a moment after the close before the aggregate settles;
without it the first read returns the still-forming bar and `fetch_bars` correctly
discards it, which would silently skip that bar entirely. It is per timeframe because 90
seconds is a pause after a daily close and a bar and a half after a one-minute one — see
`POLL_LAG_BY_TF` for what was measured.

**Dedup on bar open time.** A poll that lands early, a retry, and a vendor revision can
all re-deliver a bar already published. Nautilus will happily process a duplicate and the
strategy would trade twice, so the last published open timestamp per bar type is tracked
and anything at or before it is dropped.

**Instruments are declared, not discovered.** Twelve Data has no instrument reference
endpoint worth wiring, and the forward-test universe is fixed. `size_precision=0` on the
equities enforces whole-share fills, matching `validate.py` in the research project.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

import paper_config
import td_live

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import BarAggregation
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair, Equity
from nautilus_trader.model.objects import Price, Quantity

CLIENT_ID = ClientId("TWELVEDATA")

# Seconds after a bar boundary before the first fetch attempt. A vendor aggregate is not
# always settled the instant the interval ends.
POLL_LAG = 90
# If the settled bar has not appeared yet, retry on this cadence rather than waiting a
# whole interval and losing the bar.
RETRY_EVERY = 60
MAX_RETRIES = 20

# The two smallest sizes get their own lag, because 90 seconds is a sensible pause after a
# DAILY close and is a bar and a half after a one-minute one. At 90 the poll for a 1m bar
# fires 30 seconds after the NEXT bar has already closed, so minute bars arrived late and
# in clumps of two — which is what `MEMBER_TIMEFRAMES` gaining `1m` on 2026-08-28 made
# worth fixing. At 5m, which `BOOK_TIMEFRAMES` carries, it was 30% of a bar of pure wait.
#
# **MEASURED on 2026-08-28, and the first measurement was wrong in both directions.** Two
# traps, and anyone re-measuring this will hit both:
#
# * **The vendor serves the FORMING bar immediately.** "A bar with this stamp is present"
#   is not "this bar has settled" — the first probe read 1.1s and was timing the bar's
#   appearance, not its completion. The settle instant is when its CLOSE stops moving.
# * **The measuring machine's clock was 42 seconds slow**, taken against the vendor's own
#   `Date` header. Every "seconds after the close" figure is fiction until that is removed.
#
# Corrected: polling once a second from before each true close, the bar's close **stopped
# moving 19.7–24.0 seconds after it** — 8 of 8 one-minute bars and 8 of 8 five-minute bars,
# across BTC/USD, ETH/USD, XAU/USD and XAG/USD. A flat ~20s at both sizes reads as the
# vendor's own aggregation window rather than as network variance, which is why the same
# constant serves both.
#
# 40 seconds is the worst of those readings plus ~65% headroom, and the headroom is sized
# by what it has to cover rather than by taste: the equity classes are not in the sample
# (see below), and at 1m a lag of 40s still lands the bar inside its own minute, which is
# the property that was actually broken. Going tighter buys ten seconds on a book that
# decides once a minute and spends the entire safety margin to do it.
#
# **A shorter lag than the settle is not merely early, it is a look-ahead.** At close + 15s
# the interval HAS fully elapsed, so `fetch_bars`' forming-bar guard keeps the row — and
# its close then changes for another five seconds. The desk would have traded a print that
# was still moving, which is the same family of error as the fill-timing note in the root
# `CLAUDE.md` and just as invisible afterwards.
#
# Three more things before anyone extends this table:
#
# * **Only what was measured is listed.** `15m`, `1h`, `2h`, `4h` and `1d` keep `POLL_LAG`.
#   The ~20s looks like a fixed vendor-side finalisation and probably holds at every size,
#   and "probably" is not what the sizes carrying the live record get changed on. It also
#   costs least there: 90s is 10% of a 15m bar and 0.1% of a daily one.
# * **The equity classes are not in the sample** — it was taken at 04:32–04:40 UTC with the
#   US market shut. That is a second reason the headroom is ~65% and not 10%.
# * **The tick stream is deliberately NOT consulted here.** A socket does know when a bar
#   truly closed, but the vendor settles on a fixed ~20s delay regardless, so reading the
#   hub would buy nothing and would let a wedged WebSocket delay a bar — the one thing
#   `live_ws` may never do. A measured constant beats a dependency.
POLL_LAG_BY_TF = {"1m": 40, "5m": 40}
# Retry cadence, matched to the lag. 60 seconds is a whole bar at 1m: one slow settle would
# push the bar past the next boundary's poll, so a hiccup costs a bar rather than a
# request. This is the path WTI/USD took during the same probe — it produced no 1-minute
# bar at all on two boundaries, which is precisely what the retry loop is for.
RETRY_BY_TF = {"1m": 10, "5m": 15}


def poll_lag(timeframe: str) -> int:
    return POLL_LAG_BY_TF.get(timeframe, POLL_LAG)


def retry_every(timeframe: str) -> int:
    return RETRY_BY_TF.get(timeframe, RETRY_EVERY)


def max_retries(timeframe: str) -> int:
    """Bound the retry phase to one bar, so it can never outlive the thing it is chasing.

    `MAX_RETRIES * RETRY_EVERY` is twenty minutes, which is a reasonable slice of a daily
    bar and is twenty BARS at 1m — a retry phase that long overlaps the next twenty polls
    and hammers the vendor for a bar that was superseded nineteen times over. The bar
    itself is not lost by giving up: the next boundary's poll asks for the last three bars
    and `fresh = df[df.index > last]` delivers everything that appeared meanwhile.
    """
    span = td_live.INTERVALS[timeframe][1].total_seconds()
    return max(3, min(MAX_RETRIES, int(span // retry_every(timeframe))))
# How long a warmup pull is reused across strategies. Long enough to cover the start-up
# burst when several rules share a symbol, short enough that a restart minutes later gets
# fresh history rather than a stale frame.
WARMUP_CACHE_SECONDS = 300
# Pause before republishing the last closed bar as a live bar. Long enough that every
# strategy sharing a bar type has finished warming up, short enough that a desk started in
# the morning is holding its positions before lunch.
SEED_DELAY = 20
# Book depth to use when the vendor reports no volume (every crypto pair). Large enough
# that it never limits an order this desk would place, small enough to stay well inside a
# Quantity's precision. See `_volume` for why a zero here silently caps every fill at one
# size increment.
SYNTHETIC_VOLUME = 1_000_000_000.0


def _volume(row) -> float:
    """Bar volume — NaN-safe, and never zero, which for crypto is the whole story.

    Two separate traps, both from the same fact: **Twelve Data serves no volume field at
    all for crypto pairs**, so the column arrives as NaN rather than absent.

    First, the obvious guard does not catch it. `float(row.get("Volume") or 0.0)` returns
    `nan`, because NaN is truthy in Python — and `max(nan, 0.0)` is `nan` too, since every
    comparison against NaN is False. Nautilus then rejects the bar with `invalid value, was
    nan`, which is how ten crypto strategies once sat at "warming up" forever while the
    equities warmed normally.

    Second — and this is the subtle one — substituting a literal `0.0` fixes the rejection
    and breaks execution instead. `SandboxExecutionClientConfig` builds its exchange with
    `book_type="L1_MBP"`, so under `bar_execution` the simulated order book takes its
    **depth from the bar's volume**. A bar carrying zero volume is a market with no size in
    it, and a market order against it can only ever fill one `size_increment`: every crypto
    order came back as `last_qty=0.000001` regardless of what was requested, while equities
    — which carry real volume in the millions — filled in full. It looks exactly like a
    sizing bug in the strategy and is nothing of the kind.

    So a synthetic depth is supplied when the vendor gives none. This is safe here for a
    specific reason rather than by assumption: no rule in this project reads volume on
    crypto. `signals.usable_rules` already excludes AD, ADOSC, MFI and OBV for that class
    precisely because the data does not exist, and the vectorised research engine reads
    Close only. The number therefore feeds the simulated book and nothing else.
    """
    try:
        v = float(row.get("Volume"))
    except (TypeError, ValueError):
        v = float("nan")
    if v == v and v > 0:                          # `v == v` is False only for NaN
        return v
    return SYNTHETIC_VOLUME


def equity_instrument(symbol: str, venue: str) -> Equity:
    """Whole-share equity, matching the realism stage of the research project."""
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(venue)),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def pair_instrument(pair: str, venue: str) -> CurrencyPair:
    """`BTC/USD` or `XAU/USD` -> a fractional-quantity spot pair.

    Was `crypto_instrument`, and the rename is the point: commodities are quoted the same
    way and settle the same way — buying `XAU/USD` converts USD into XAU exactly as buying
    `BTC/USD` converts it into BTC — so they want the same instrument, on their own venue.
    Nautilus's currency registry already knows XAU, XAG, XPT, XPD and WTI, so nothing has to
    be invented for them.
    """
    from nautilus_trader.model.currencies import BTC, ETH, USD as _USD
    from nautilus_trader.model.objects import Currency

    base_code = pair.split("/")[0]
    known = {"BTC": BTC, "ETH": ETH}
    base = known.get(base_code) or Currency.from_str(base_code)
    symbol = pair.replace("/", "")
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol(symbol), Venue(venue)),
        raw_symbol=Symbol(symbol),
        base_currency=base,
        quote_currency=_USD,
        price_precision=2,
        size_precision=6,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.000001"),
        # Stated explicitly to match `../backtest engine/engines/nautilus.py`, so the paper
        # instrument and the parity instrument are the same shape. These were NOT the cause
        # of the one-increment fills — that was zero bar volume starving the L1 book, see
        # `_volume` — but leaving them implicit invites the same investigation twice.
        lot_size=None,
        max_quantity=None,
        min_quantity=None,
        max_notional=None,
        min_notional=None,
        max_price=None,
        min_price=None,
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


def futures_instrument(symbol: str, venue: str) -> CurrencyPair:
    """`ES.v.0` -> a FRACTIONAL instrument, and the word "contract" here is a lie.

    **A unit on this leg is a fractional notional unit of a back-adjusted continuous
    series, not a CME contract**, and that has to be said plainly because a reader who
    sees "futures" will assume otherwise. Two facts force it:

    * The series itself is a fiction. `ES.v.0` is whichever contract carried the most
      volume that day, ratio back-adjusted across every roll, so a level on it is not a
      price anybody paid and a "quantity" of it is not a position anybody could open.
      `data/reference/futures_rolls.csv` records every adjustment behind it.
    * Nautilus's `FuturesContract` has no `size_increment` — quantities are whole
      contracts — and this desk's `BOOK_CAPITAL` is $100,000 split across a class's names.
      Nineteen roots is ~$5,263 a slice, and $5,263 against ES at ~$385,000 of index
      exposure per contract rounds to **zero**. The whole book would sit flat while every
      log line read healthy, which is this folder's worst failure mode and it has happened
      before.

    The research book holds fractional equal weights anyway, so the fractional instrument
    is the one that reproduces what was measured. What it does NOT reproduce is contract
    sizing: `futures_specs.CME_CONTRACTS` carries the multiplier, the quote scale and the
    tick, and none of them are used here. A number on this leg is a notional exposure, and
    it must not be read as a contract count.

    Shape is `pair_instrument`'s, for the same reason commodities use it: buying settles
    USD into the base asset. `Currency.from_str("ES")` auto-creates an 8-precision
    currency, so no registration step is needed for a root Nautilus has never heard of.
    """
    from nautilus_trader.model.currencies import USD as _USD
    from nautilus_trader.model.objects import Currency

    root = symbol.split(".")[0]
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol(symbol), Venue(venue)),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(root),
        quote_currency=_USD,
        # Six places rather than two, because a back-adjusted price is a real price times
        # a product of ratios and lands nowhere near the exchange's tick grid. Rounding it
        # to the grid would be a fiction about a fiction; the tick that matters lives in
        # `futures_specs` and is not what this series is quoted on.
        price_precision=6,
        size_precision=6,
        price_increment=Price.from_str("0.000001"),
        size_increment=Quantity.from_str("0.000001"),
        lot_size=None,
        max_quantity=None,
        min_quantity=None,
        max_notional=None,
        min_notional=None,
        max_price=None,
        min_price=None,
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


# Which builder each class gets. Held as a map rather than as a chain of `if`s because
# five call sites used to carry their own copy of the chain — `run_paper.instrument_for`,
# `desk_control._build`, `member_strategy._instrument_for` and `book_strategy` twice — and
# the futures leg needed a third branch in every one of them. A class whose instrument
# shape is decided in five places is a class that will be one shape in four of them.
_BUILDERS = {"cme_futures": futures_instrument}


def instrument_for(symbol: str, asset_class: str, venue: str):
    """The Nautilus instrument for a desk symbol, by CLASS rather than by spelling.

    Never inferred from the ticker. `XAU/USD` carries the same separator as `BTC/USD` and
    belongs to a different class, a different venue and a different leaderboard; `ES.v.0`
    carries no separator at all and is not a share. The class is looked up.
    """
    builder = _BUILDERS.get(asset_class)
    if builder is not None:
        return builder(symbol, venue)
    if asset_class in paper_config.PAIR_CLASSES:
        return pair_instrument(symbol, venue)
    return equity_instrument(symbol, venue)


def vendor_symbol(instrument_id: InstrumentId) -> str:
    """`BTCUSD.BINANCE` -> `BTC/USD`; `XAUUSD.SPOT` -> `XAU/USD`; `SOXL.SANDBOX` -> `SOXL`.

    A lookup in `paper_config.SAFE_TO_VENDOR`, not a pattern match on the ticker. The old
    shape — strip a `USD` suffix if the head looks like a currency code — searched only the
    crypto leg, so `XAUUSD` fell through to itself and every commodity bar request asked
    Twelve Data for an instrument that does not exist. That fails *quietly*: the vendor
    answers with an empty series rather than an error, so the strategy would have warmed up
    forever on zero bars while its logs looked healthy.
    """
    s = instrument_id.symbol.value
    return paper_config.SAFE_TO_VENDOR.get(s, s)


# Nautilus aggregation -> the suffix this project spells a timeframe with. `INTERVALS` is
# the authority on which of the resulting keys the vendor can actually serve.
_TF_UNIT = {
    BarAggregation.DAY: "d",
    BarAggregation.HOUR: "h",
    BarAggregation.MINUTE: "m",
}


def timeframe_of(bar_type: BarType) -> str:
    """Map a Nautilus bar spec back to this project's timeframe key.

    `spec.aggregation` is an int at runtime, not the enum member and not a string, so
    this compares against `BarAggregation` rather than pattern-matching a repr.

    **Derived from `td_live.INTERVALS`, not from a list of branches.** It was two
    hardcoded cases — `1d` and `4h` — while `paper_config.MEMBER_TIMEFRAMES` offered six
    and `/v1/limits` advertised all six to managers. The failure that produced was as
    quiet as it gets: a member registers at `5m`, the API returns 201, `_attach` accepts
    it because the timeframe IS in `MEMBER_TIMEFRAMES`, the strategy attaches and logs
    `RUNNING`, the desk marks the registration `live` — and then `_subscribe_bars` raises
    in here, inside a Nautilus task, where it is logged as an ERROR and goes no further.
    No bar ever arrives, so `_last_price` stays empty, so **every order that strategy ever
    sends is rejected** with "no price for BTC/USD yet ... try again after the next 5m
    close", which is advice that cannot come true. Two strategies sat like that for
    fifteen hours, reading `live` in the console the whole time.

    Nothing else needed changing: `td_live.INTERVALS` already carries `5min`, `15min`,
    `1h` and `2h`, `_interval_delta` reads the step from it, and
    `_seconds_to_next_close` is modular arithmetic over that step. The two branches were
    the only thing in the way.
    """
    spec = bar_type.spec
    unit = _TF_UNIT.get(spec.aggregation)
    key = f"{spec.step}{unit}" if unit else None
    # PRESENT IS NOT FEEDABLE. `INTERVALS` carries a row for every timeframe the repo
    # knows, and since 2026-08-21 some of those rows have a vendor interval of `None`:
    # `2m` and `3m` are RESAMPLED from cached 1m for the backtest and Twelve Data sells
    # no such product. A membership test alone would hand the live client a key it can
    # spell and cannot subscribe to, which is the exact fifteen-hour failure described
    # above wearing a new hat. The vendor interval is the capability; test that.
    if key in td_live.INTERVALS and td_live.INTERVALS[key][0] is not None:
        return key
    raise ValueError(f"unsupported bar spec for this forward test: {spec}")


# The offer and the capability, checked against each other at import.
#
# `paper_config` cannot do this itself — it is imported by `Stockhunt Dashboard/`, which
# must not drag `nautilus_trader` into a build — so the check lives here, in the module
# that owns the capability, and fires when the DESK starts rather than on somebody's first
# order. This is the guard `paper_config.MEMBER_TIMEFRAMES` documents; it was previously
# made against `BAR_SPEC`, which is derived from the BACKTEST engine's timeframe list and
# therefore says nothing whatever about what the live vendor client can subscribe to.
_unfeedable = [tf for tf in paper_config.MEMBER_TIMEFRAMES
               if td_live.INTERVALS.get(tf, (None,))[0] is None]
if _unfeedable:
    raise SystemExit(
        f"{', '.join(_unfeedable)} is offered in paper_config.MEMBER_TIMEFRAMES but "
        f"td_live.INTERVALS cannot feed it. A timeframe a manager can register at and "
        f"the desk cannot subscribe to is a strategy that reads `live` and can never "
        f"trade.")

# The house's own books are no longer all 1d/4h, so they need the same check. A book
# timeframe the client cannot feed fails inside a Nautilus task at subscribe time, which
# is logged and goes nowhere — the same silence, on the desk's own capital.
_unfeedable_books = [tf for tf in paper_config.BOOK_TIMEFRAMES
                     if td_live.INTERVALS.get(tf, (None,))[0] is None]
if _unfeedable_books:
    raise SystemExit(
        f"{', '.join(_unfeedable_books)} is in paper_config.BOOK_TIMEFRAMES but "
        f"td_live.INTERVALS cannot feed it.")


def _interval_delta(timeframe: str) -> timedelta:
    return td_live.INTERVALS[timeframe][1]


class TwelveDataDataClientConfig(LiveDataClientConfig, frozen=True):
    """`window_bars` is the warmup handed to a strategy on request; it must be at least
    `paper_config.MEASURED_WINDOW_BARS` or a recursive rule will not match the backtest."""

    window_bars: int = paper_config.DEFAULT_WINDOW_BARS


class TwelveDataLiveClient(LiveMarketDataClient):
    def __init__(self, loop, msgbus: MessageBus, cache: Cache, clock: LiveClock,
                 config: TwelveDataDataClientConfig) -> None:
        super().__init__(
            loop=loop,
            client_id=CLIENT_ID,
            venue=None,                      # routes for several venues
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            config=config,
        )
        self._window = int(getattr(config, "window_bars",
                                   paper_config.DEFAULT_WINDOW_BARS))
        self._poll_tasks: dict[BarType, asyncio.Task] = {}
        self._last_open: dict[BarType, pd.Timestamp] = {}
        self._warmup_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self._warmup_locks: dict[tuple, threading.Lock] = {}
        self._seeded: set = set()

    # ------------------------------------------------------------------ lifecycle
    async def _connect(self) -> None:
        td_live.api_key()                    # fail fast on a missing key
        self._log.info(f"Twelve Data connected, warmup window {self._window} bars")

    async def _disconnect(self) -> None:
        for task in self._poll_tasks.values():
            task.cancel()
        self._poll_tasks.clear()

    # ------------------------------------------------------------------ conversion
    def _to_bar(self, bar_type: BarType, ts: pd.Timestamp, row) -> Bar | None:
        inst = self._cache.instrument(bar_type.instrument_id)
        if inst is None:
            self._log.error(f"no instrument in cache for {bar_type.instrument_id}")
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        # A bar is stamped at its CLOSE for Nautilus, while the vendor stamps the open.
        close_ns = int((ts + _interval_delta(timeframe_of(bar_type))).value)
        try:
            return Bar(
                bar_type=bar_type,
                open=inst.make_price(float(row["Open"])),
                high=inst.make_price(float(row["High"])),
                low=inst.make_price(float(row["Low"])),
                close=inst.make_price(float(row["Close"])),
                volume=inst.make_qty(_volume(row)),
                ts_event=close_ns,
                ts_init=close_ns,
            )
        except Exception as exc:
            self._log.error(f"bad bar for {bar_type} at {ts}: {exc}")
            return None

    def _frame(self, bar_type: BarType, n: int) -> pd.DataFrame:
        """Warmup history, cached briefly per (bar_type, size).

        Every strategy requests its own warmup at start-up, so running five rules on one
        symbol asked the vendor for the same 1,500 bars five times inside a few seconds.
        With five rules across five symbols that is 25 identical-by-symbol pulls against a
        credit budget, to build five identical DataFrames. The TTL only has to outlive the
        start-up burst — beyond that the poller, not this path, supplies new bars.
        """
        key = (str(bar_type), n)

        def fresh():
            hit = self._warmup_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < WARMUP_CACHE_SECONDS:
                return hit[1]
            return None

        cached = fresh()
        if cached is not None:
            return cached

        # Serialise per key. Every strategy asks for its warmup at start-up and the
        # requests are dispatched concurrently, so a plain check-then-fetch cache misses
        # every time — with five rules on one symbol all five were already in flight
        # before the first response landed, and the cache recorded zero hits. Holding a
        # lock means the first caller fetches and the rest wait and read its result.
        lock = self._warmup_locks.setdefault(key, threading.Lock())
        with lock:
            cached = fresh()
            if cached is not None:
                self._log.info(f"warmup cache hit for {bar_type} ({n} bars)")
                return cached
            df = td_live.fetch_bars(vendor_symbol(bar_type.instrument_id),
                                    timeframe_of(bar_type), n=n)
            self._warmup_cache[key] = (time.monotonic(), df)
            return df

    # ------------------------------------------------------------------ requests
    async def _request_bars(self, request) -> None:
        """Historical warmup. The strategy asks once at start and computes from this."""
        bar_type = request.bar_type
        limit = request.limit or self._window
        try:
            df = await asyncio.to_thread(self._frame, bar_type, max(limit, self._window))
        except Exception as exc:
            self._log.error(f"warmup request failed for {bar_type}: {exc}")
            df = pd.DataFrame()

        bars = [b for b in (self._to_bar(bar_type, ts, row)
                            for ts, row in df.iterrows()) if b is not None]
        if bars:
            self._last_open[bar_type] = df.index[-1]
        self._log.info(f"warmup {bar_type}: {len(bars)} bars")
        # Signature is (bar_type, bars, correlation_id, start, end, params) — the
        # correlation id comes third, not after the range.
        self._handle_bars(bar_type, bars, request.id, request.start, request.end,
                          request.params)

        if bars and bar_type not in self._seeded:
            self._seeded.add(bar_type)
            self.create_task(self._seed_market(bar_type, bars[-1]))

    async def _seed_market(self, bar_type: BarType, bar: Bar) -> None:
        """Republish the last closed bar on the live channel, once per bar type.

        Warmup arrives as a *request response*, which the sandbox venue never sees — it
        only listens on the live data channel. So until a genuinely new bar closed, the
        exchange had no market and any order was rejected with `no market for <symbol>`.
        A daily strategy therefore sat flat until the next session even though its rule
        already knew what it wanted to hold, and the P&L it published measured a position
        it had not taken.

        Publishing the last closed bar once fixes both halves at a stroke: the exchange
        gets a price, and every strategy subscribed to this bar type runs its ordinary
        `on_bar` path against a buffer that is already warm — so it opens the position the
        rule currently calls for and starts tracking from there.

        Two details that matter. The delay lets every strategy sharing this bar type finish
        its own warmup first; one that has not would see the bar, find its buffer short and
        skip. And `_last_open` was already advanced by the warmup, so the poller will not
        re-publish this bar — while `_append` in the strategy discards anything at or
        before the newest bar it holds, which makes a duplicate harmless either way.
        """
        try:
            await asyncio.sleep(SEED_DELAY)
            self._log.info(f"seeding market for {bar_type} from its last closed bar")
            self._handle_data(bar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error(f"could not seed {bar_type}: {exc}")

    # ------------------------------------------------------------------ streaming
    async def _subscribe_bars(self, command) -> None:
        bar_type = command.bar_type
        if bar_type in self._poll_tasks:
            return
        tf = timeframe_of(bar_type)          # reject unsupported specs loudly, now
        self._poll_tasks[bar_type] = self.create_task(self._poll(bar_type))
        self._log.info(f"subscribed {bar_type} (poll at close + {poll_lag(tf)}s)")

    async def _unsubscribe_bars(self, command) -> None:
        task = self._poll_tasks.pop(command.bar_type, None)
        if task:
            task.cancel()

    def _seconds_to_next_close(self, timeframe: str) -> float:
        delta = _interval_delta(timeframe)
        now = datetime.now(timezone.utc)
        step = delta.total_seconds()
        epoch = now.timestamp()
        return step - (epoch % step)

    async def _poll(self, bar_type: BarType) -> None:
        timeframe = timeframe_of(bar_type)
        lag, retry, tries = (poll_lag(timeframe), retry_every(timeframe),
                             max_retries(timeframe))
        while True:
            try:
                await asyncio.sleep(self._seconds_to_next_close(timeframe) + lag)
                for _ in range(tries):
                    try:
                        df = await asyncio.to_thread(self._frame, bar_type, 3)
                    except Exception as exc:
                        self._log.warning(f"{bar_type} poll error: {exc}")
                        df = pd.DataFrame()
                    if len(df):
                        last = self._last_open.get(bar_type)
                        fresh = df[df.index > last] if last is not None else df.tail(1)
                        if len(fresh):
                            for ts, row in fresh.iterrows():
                                bar = self._to_bar(bar_type, ts, row)
                                if bar is not None:
                                    self._handle_data(bar)
                            self._last_open[bar_type] = fresh.index[-1]
                            break
                    await asyncio.sleep(retry)
                else:
                    self._log.warning(
                        f"{bar_type}: no settled bar after "
                        f"{tries * retry}s — will wait for the next close")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(f"{bar_type} poll loop error: {exc}")
                await asyncio.sleep(retry)


class TwelveDataLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock) -> TwelveDataLiveClient:
        return TwelveDataLiveClient(loop=loop, msgbus=msgbus, cache=cache,
                                    clock=clock, config=config)
