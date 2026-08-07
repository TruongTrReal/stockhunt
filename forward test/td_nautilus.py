"""Twelve Data as a Nautilus live data client, plus the instruments to trade against.

This is the piece that makes `backtest -> paper -> live` one code path instead of three
codebases. Nautilus already runs the backtest (`../backtest master/engines/nautilus.py`);
here the same engine is fed live bars from the same vendor the research used, and paired
with `SandboxExecutionClient` — real prices in, simulated fills out. Going live later
swaps the execution client and touches nothing else.

Design notes worth keeping:

**Bars, not ticks.** `SandboxExecutionClient(bar_execution=True)` fills from bars, so a
1d/4h strategy needs no tick feed at all. See `td_live` for why polling beats streaming
at these horizons.

**One poll task per subscription, aligned to the close.** The task sleeps until the next
bar boundary plus `POLL_LAG`, then fetches. The lag exists because a vendor stamps a bar
at its open and needs a moment after the close before the aggregate settles; without it
the first read returns the still-forming bar and `fetch_bars` correctly discards it,
which would silently skip that bar entirely.

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

import fwd_config
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


def crypto_instrument(pair: str, venue: str) -> CurrencyPair:
    """`BTC/USD` -> a fractional-quantity spot pair."""
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
        # Stated explicitly to match `../backtest master/engines/nautilus.py`, so the paper
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


def vendor_symbol(instrument_id: InstrumentId) -> str:
    """`BTCUSD.BINANCE` -> `BTC/USD`; `SOXL.SANDBOX` -> `SOXL`."""
    s = instrument_id.symbol.value
    if s.endswith("USD") and len(s) > 3 and s not in ("USD",):
        head = s[:-3]
        if head.isalpha() and head.upper() == head and len(head) <= 5 and "/" not in s:
            for pair in fwd_config.CRYPTO_SYMBOLS:
                if pair.replace("/", "") == s:
                    return pair
    return s


def timeframe_of(bar_type: BarType) -> str:
    """Map a Nautilus bar spec back to this project's timeframe key.

    `spec.aggregation` is an int at runtime, not the enum member and not a string, so
    this compares against `BarAggregation` rather than pattern-matching a repr.
    """
    spec = bar_type.spec
    if spec.aggregation == BarAggregation.DAY and spec.step == 1:
        return "1d"
    if spec.aggregation == BarAggregation.HOUR and spec.step == 4:
        return "4h"
    raise ValueError(f"unsupported bar spec for this forward test: {spec}")


def _interval_delta(timeframe: str) -> timedelta:
    return td_live.INTERVALS[timeframe][1]


class TwelveDataDataClientConfig(LiveDataClientConfig, frozen=True):
    """`window_bars` is the warmup handed to a strategy on request; it must be at least
    `fwd_config.MEASURED_WINDOW_BARS` or a recursive rule will not match the backtest."""

    window_bars: int = fwd_config.DEFAULT_WINDOW_BARS


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
                                   fwd_config.DEFAULT_WINDOW_BARS))
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
        timeframe_of(bar_type)               # reject unsupported specs loudly, now
        self._poll_tasks[bar_type] = self.create_task(self._poll(bar_type))
        self._log.info(f"subscribed {bar_type} (poll at close + {POLL_LAG}s)")

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
        while True:
            try:
                await asyncio.sleep(self._seconds_to_next_close(timeframe) + POLL_LAG)
                for _ in range(MAX_RETRIES):
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
                    await asyncio.sleep(RETRY_EVERY)
                else:
                    self._log.warning(
                        f"{bar_type}: no settled bar after "
                        f"{MAX_RETRIES * RETRY_EVERY}s — will wait for the next close")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(f"{bar_type} poll loop error: {exc}")
                await asyncio.sleep(RETRY_EVERY)


class TwelveDataLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock) -> TwelveDataLiveClient:
        return TwelveDataLiveClient(loop=loop, msgbus=msgbus, cache=cache,
                                    clock=clock, config=config)
