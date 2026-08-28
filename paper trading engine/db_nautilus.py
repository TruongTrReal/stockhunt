"""Databento as a Nautilus live data client, for the `GLBX` venue and nothing else.

The second data client on this desk, and the seam it plugs into is Nautilus's own:
`DataEngine.register_client` files a client with `venue=None` as `_default_client` and one
with a concrete venue into `_routing_map[venue]`. `TwelveDataLiveClient` passes `None` and
stays the default; this one passes `Venue("GLBX")` and therefore receives **exactly** the
futures subscriptions and nothing else. No routing code had to be written, and — the part
that matters — no futures bar can reach Twelve Data by accident, which is the failure the
root `CLAUDE.md` spends a section on: an unqualified `ES` there is Eversource Energy,
returned as a clean, plausible, entirely wrong series.

That is also why this client is registered **even when there is no Databento key**. Leaving
it out does not disable the futures leg, it hands the leg to the default client — which
would ask Twelve Data for `ES.v.0` and get a wrong instrument or an empty frame back. A
registered client with no credential refuses visibly; an absent one mis-routes silently.

Everything else is `td_nautilus`'s shape, deliberately: one poll task per subscription
aligned to the bar close, dedup on the bar's open time, a briefly-cached warm-up shared
across the strategies that ask for it at once, and the last closed bar republished once so
the sandbox exchange has a market to fill against.

**The one thing that is genuinely new is the roll.**

`data/futures/**` is ratio back-adjusted; a live poll is not. On a roll date the raw
continuous series steps to a different contract's level, and `book_strategy` appends live
bars to a rolling buffer, so an unhandled roll injects a return nobody earned straight into
the signal. The fix is exact rather than approximate, and it rests on one fact:
back-adjustment is multiplication by a constant, and every price indicator in this repo is
equivariant under a common scale. So the anchor does not matter and internal consistency
does.

    warm-up      `db_live.fetch_bars` adjusts the window through `db_loader.back_adjust`,
                 anchored at the newest bar — the same code and the same rank-0/rank-1
                 same-bar ratio the cache was built with.
    afterwards   a per-symbol cumulative FORWARD factor, 1.0 at warm-up. A roll is
                 detected the way `db_loader` detects one — the `instrument_id` behind the
                 continuous symbol changing — its ratio is read from rank 0 and rank 1 on
                 the SAME bar, and the factor is divided by it. Every emitted bar is
                 multiplied by the factor.

Divided, not multiplied, and the direction is worth spelling out. `back_adjust` scales
*history* up to the newest contract's level. Here history is already published and cannot
be rewritten, so it is the new bars that come down to the anchor the warm-up chose. Same
adjustment, opposite end.

If the ratio cannot be measured exactly the fallback is the ordinary close-to-close splice
— the same fallback `db_loader` records as `close-to-close splice` in the roll ledger — and
it is logged as a warning naming the contracts. **An unadjusted bar is never emitted across
a roll**, silently or otherwise.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pandas as pd

import paper_config
import db_live
import td_nautilus

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import ClientId, Venue

CLIENT_ID = ClientId("DATABENTO")

# The class this client exists for, and the venue Nautilus routes to it. `GLBX` is
# Databento's own name for the CME Globex dataset, which is the same reason `BINANCE` and
# `SPOT` are spelled the way they are: the venue on the instrument is the venue in the
# route, and `run_paper.route_bars_to_sandbox` filters on it.
ASSET_CLASS = "cme_futures"
VENUE = Venue(paper_config.VENUES.get(ASSET_CLASS, "GLBX"))

# Seconds after a bar boundary before the first fetch attempt.
#
# **Sized against a measurement, not a guess.** The historical archive lags real time by
# about eight minutes (`db_live.ARCHIVE_LAG_SECONDS`, probed 2026-08-27 at 12:58:10 UTC:
# the dataset ended at 12:50:00 UTC). Poll inside that window and the vendor has nothing
# settled, `drop_forming` correctly discards what it does have, and the bar is skipped
# with nothing in the log to say a bar was skipped rather than not yet due. Fifteen
# minutes is that lag plus most of it again — the retry loop below covers the rest, and at
# 1d and 1h a quarter of an hour costs a book nothing.
POLL_LAG = 15 * 60

# ...except at 1m, where fifteen minutes would be fifteen BARS. The lag has to clear the
# archive frontier and nothing more, so it is sized on the worst sampled reading (7.3 min,
# 2026-08-28) plus headroom, and the retry loop below covers the rest. A minute bar still
# reaches the desk about eight minutes after it closed — that is the vendor's floor
# without a LIVE subscription, and it is a property of this class that has to be said out
# loud rather than hidden in a constant. See `desk_control._feedable`, which says it to
# the member who registered.
POLL_LAG_BY_TF = {"1m": 8 * 60}


def poll_lag(timeframe: str) -> int:
    """Seconds after a bar boundary before the first fetch. Per timeframe, because a lag
    sized for a daily bar is most of a session at 1m."""
    return POLL_LAG_BY_TF.get(timeframe, POLL_LAG)
# If the settled bar has not appeared yet, retry on this cadence rather than waiting a
# whole interval and losing the bar. Two minutes rather than `td_nautilus`'s one, because
# a Databento window costs ~25 seconds of server-side symbology resolution whatever it
# carries — a tighter loop would spend its time in flight rather than waiting.
RETRY_EVERY = 120
MAX_RETRIES = 20
# How many bars a poll asks for. Enough to bridge a missed close or two, and — the reason
# it is not 3 — enough that a roll's own bar and the bar before it are both in the window,
# which is what makes the same-bar ratio measurable at all.
POLL_BARS = 8
# Shared with `td_nautilus` so the two clients' warm-up caching and market seeding behave
# identically; both numbers were chosen for the start-up burst, which is the same burst.
WARMUP_CACHE_SECONDS = td_nautilus.WARMUP_CACHE_SECONDS
SEED_DELAY = td_nautilus.SEED_DELAY


def timeframe_of(bar_type: BarType) -> str:
    """Map a Nautilus bar spec back to a timeframe this vendor can actually serve.

    Two steps, and the second one is the whole point. `td_nautilus._TF_UNIT` turns the
    spec into this project's key — reused rather than restated, because two tables that
    both look authoritative is how the desk once offered six timeframes and could feed
    two. Then `db_live.can_feed` decides, because **present is not feedable**: the GLBX
    archive has no 15m or 4h schema at all, and its 1m bars carry the folded-session
    defect before 2016. A key that can be spelled and not subscribed to is the
    fifteen-hour failure `td_nautilus.timeframe_of`'s docstring describes — a strategy
    that attaches, reads `live`, and has every order it ever sends refused for want of a
    price that was never going to arrive.
    """
    spec = bar_type.spec
    unit = td_nautilus._TF_UNIT.get(spec.aggregation)
    key = f"{spec.step}{unit}" if unit else None
    if key is not None and db_live.can_feed(key):
        return key
    raise ValueError(
        f"{spec} is not a bar Databento can serve for {ASSET_CLASS}: it has "
        f"{', '.join(sorted(db_live.SCHEMA))} and nothing else. The GLBX archive carries "
        f"no 15m or 4h schema, and its 1m bars fold whole sessions before 2016.")


def futures_instrument(symbol: str, venue: str):
    """A CME root as a FRACTIONAL instrument. See `td_nautilus.futures_instrument`.

    Re-exported here so a reader who arrives at the feed finds the instrument beside it,
    while the definition stays in `td_nautilus` with the other two shapes — `member_strategy`
    and `book_strategy` build instruments and must not have to import a vendor client to
    do it.
    """
    return td_nautilus.futures_instrument(symbol, venue)


class DatabentoDataClientConfig(LiveDataClientConfig, frozen=True):
    """`window_bars` is the warm-up handed to a strategy on request.

    Same contract as `TwelveDataDataClientConfig`: it must be at least
    `paper_config.MEASURED_WINDOW_BARS` or a recursive rule computes a different signal
    from the backtest with nothing to indicate it.
    """

    window_bars: int = paper_config.DEFAULT_WINDOW_BARS


class DatabentoLiveClient(LiveMarketDataClient):
    """Live CME futures bars, back-adjusted to the anchor its own warm-up chose."""

    def __init__(self, loop, msgbus: MessageBus, cache: Cache, clock: LiveClock,
                 config: DatabentoDataClientConfig) -> None:
        super().__init__(
            loop=loop,
            client_id=CLIENT_ID,
            venue=VENUE,                     # routes ONLY futures here. See the docstring
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
        # The roll state, per SYMBOL rather than per bar type: a root rolls once, and a 1d
        # and a 1h subscription on the same root are looking at the same contract change.
        self._factor: dict[str, float] = {}
        self._contract: dict[str, int] = {}
        # Set at connect. False means there is no credential, and every subscription is
        # then refused with a sentence instead of started and left silent.
        self._usable = False

    # ------------------------------------------------------------------ lifecycle
    async def _connect(self) -> None:
        """Never raises. The desk holds live positions on four other classes.

        `run_paper.py` runs under systemd with a restart policy, so an exception here is
        not "the futures leg is off", it is a restart loop that flattens every book on the
        desk. The credential is checked, the answer is logged loudly, and the client
        connects either way — it still has to own the `GLBX` venue, or futures bars fall
        through to the default Twelve Data client and get somebody else's instrument.
        """
        self._usable = db_live.have_key()
        if not self._usable:
            self._log.error(db_live.NO_KEY)
            return
        self._log.info(f"Databento {db_live.DATASET} connected, warmup window "
                       f"{self._window} bars, poll at close + {POLL_LAG}s "
                       f"({POLL_LAG_BY_TF.get('1m')}s at 1m) "
                       f"(archive lags ~{db_live.ARCHIVE_LAG_SECONDS // 60} min)")

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
        # Nautilus stamps a bar at its CLOSE; Databento stamps it at its open.
        close_ns = int((ts + db_live.INTERVALS[timeframe_of(bar_type)]).value)
        try:
            return Bar(
                bar_type=bar_type,
                open=inst.make_price(float(row["Open"])),
                high=inst.make_price(float(row["High"])),
                low=inst.make_price(float(row["Low"])),
                close=inst.make_price(float(row["Close"])),
                volume=inst.make_qty(td_nautilus._volume(row)),
                ts_event=close_ns,
                ts_init=close_ns,
            )
        except Exception as exc:                    # noqa: BLE001 - logged, never fatal
            self._log.error(f"bad bar for {bar_type} at {ts}: {exc}")
            return None

    # ------------------------------------------------------------------ adjustment
    def _publish_factor(self, symbol: str, factor: float) -> None:
        """Tell `db_live` what scale this desk is on, so the MARK matches the FILLS.

        The mark-to-market poller reads the vendor fresh, which anchors at the vendor's
        newest bar; the desk's bars are anchored where the warm-up left them. After a roll
        those differ by that roll's ratio — a median 0.56% on this universe — applied to
        the whole position, which shows up as a P&L step nobody traded.
        """
        self._factor[symbol] = factor
        db_live.FORWARD_FACTORS[symbol] = factor

    def _apply_roll(self, symbol: str, front: pd.DataFrame, behind: pd.DataFrame,
                    new_id: int) -> None:
        """Fold one roll into this symbol's forward factor.

        The ratio comes from `db_live.roll_ratios`, which is `db_loader.back_adjust` — the
        same same-bar rank-1 rule, including its check that rank 1 really was the contract
        rank 0 became. When that check fails the ledger's own fallback is the close-to-close
        splice, and it is said out loud here rather than recorded quietly: a splice folds a
        session of market movement into the adjustment, and on this leg the number it
        distorts is a live position's entry.
        """
        old_id = self._contract.get(symbol)
        ratio, method = None, "unmeasured"
        try:
            ratios = db_live.roll_ratios(front, behind, symbol)
            hit = ratios.get((int(old_id), int(new_id))) if old_id is not None else None
            if hit is not None:
                ratio, method = hit
        except Exception as exc:                    # noqa: BLE001 - reported below
            self._log.error(f"{symbol}: could not price the roll {old_id} -> {new_id}: "
                            f"{exc}")
        if ratio is None or not (ratio > 0):
            # Last resort, and it is still an adjustment: emitting the raw bar would hand
            # the strategy the whole roll gap as a return. WTI's was +37% in April 2020.
            closes = front["Close"].to_numpy()
            ratio = float(closes[-1] / closes[-2]) if len(closes) > 1 and closes[-2] > 0 \
                else 1.0
            method = "close-to-close splice (INEXACT)"
        if method != "same-bar rank 1":
            self._log.warning(
                f"{symbol}: rolled {old_id} -> {new_id} and the exact same-bar ratio was "
                f"not measurable; adjusting by {ratio:.6f} via {method}. The live buffer "
                f"stays continuous, but this roll is not as clean as the cache's.")
        else:
            self._log.info(f"{symbol}: rolled {old_id} -> {new_id}, ratio {ratio:.6f} "
                           f"({method})")
        self._contract[symbol] = int(new_id)
        self._publish_factor(symbol, self._factor.get(symbol, 1.0) / ratio)

    # ------------------------------------------------------------------ requests
    def _frame(self, bar_type: BarType, n: int) -> pd.DataFrame:
        """Adjusted warm-up history, cached briefly per (bar_type, size).

        Same TTL cache and same per-key lock as `td_nautilus._frame`, and it matters more
        here: every strategy on a symbol asks for its own warm-up in the same second, and
        a Databento window costs ~25 seconds of server-side symbology resolution whatever
        it carries. Without the lock a plain check-then-fetch cache records zero hits,
        because all of the requests are already in flight before the first response lands.

        The RAW frames are fetched, the roll anchor is recorded from them, and the
        adjusted frame is what is returned — one pull, both jobs, rather than asking the
        vendor twice for the same window.
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

        lock = self._warmup_locks.setdefault(key, threading.Lock())
        with lock:
            cached = fresh()
            if cached is not None:
                self._log.info(f"warmup cache hit for {bar_type} ({n} bars)")
                return cached
            symbol = bar_type.instrument_id.symbol.value
            front, behind = db_live.fetch_raw(symbol, timeframe_of(bar_type), n=n)
            if front.empty:
                return pd.DataFrame()
            import db_loader
            bars, _ = db_loader.back_adjust(front, behind, symbol)
            # The anchor. Every bar emitted from here on is scaled to this window's newest
            # contract, so the factor starts at 1.0 and the buffer stays one series.
            self._contract[symbol] = int(front["instrument_id"].iloc[-1])
            self._publish_factor(symbol, 1.0)
            self._warmup_cache[key] = (time.monotonic(), bars)
            return bars

    async def _request_bars(self, request) -> None:
        """Historical warm-up, back-adjusted the same way the cache is."""
        bar_type = request.bar_type
        limit = request.limit or self._window
        if not self._usable:
            self._log.error(f"{bar_type}: {db_live.NO_KEY}")
            self._handle_bars(bar_type, [], request.id, request.start, request.end,
                              request.params)
            return
        try:
            df = await asyncio.to_thread(self._frame, bar_type, max(limit, self._window))
        except Exception as exc:                    # noqa: BLE001 - logged, never fatal
            self._log.error(f"warmup request failed for {bar_type}: {exc}")
            df = pd.DataFrame()

        bars = [b for b in (self._to_bar(bar_type, ts, row)
                            for ts, row in df.iterrows()) if b is not None]
        if bars:
            self._last_open[bar_type] = df.index[-1]
        self._log.info(f"warmup {bar_type}: {len(bars)} bars")
        self._handle_bars(bar_type, bars, request.id, request.start, request.end,
                          request.params)

        if bars and bar_type not in self._seeded:
            self._seeded.add(bar_type)
            self.create_task(self._seed_market(bar_type, bars[-1]))

    async def _seed_market(self, bar_type: BarType, bar: Bar) -> None:
        """Republish the last closed bar on the live channel, once per bar type.

        The same fix, and the same reason, as `td_nautilus._seed_market`: warm-up arrives
        as a request response, which the sandbox venue never sees — it listens on the live
        data channel only. Without this the exchange has no market for the instrument and
        every order is rejected with `no market for <symbol>`, so a daily book sits flat
        until the next session while its rule already knows what it wants to hold.
        """
        try:
            await asyncio.sleep(SEED_DELAY)
            self._log.info(f"seeding market for {bar_type} from its last closed bar")
            self._handle_data(bar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                    # noqa: BLE001 - logged, never fatal
            self._log.error(f"could not seed {bar_type}: {exc}")

    # ------------------------------------------------------------------ streaming
    async def _subscribe_bars(self, command) -> None:
        bar_type = command.bar_type
        if bar_type in self._poll_tasks:
            return
        # Loudly, and BEFORE a task exists. A ValueError raised inside the poll task is
        # logged as an ERROR and goes nowhere, which leaves a strategy reading `live` with
        # no bar that can ever arrive.
        timeframe_of(bar_type)
        if not self._usable:
            self._log.error(
                f"refusing to subscribe {bar_type}: {db_live.NO_KEY}. Nothing will be "
                f"polled for it, so no strategy on this class can appear to be waiting "
                f"for a bar that is coming.")
            return
        self._poll_tasks[bar_type] = self.create_task(self._poll(bar_type))
        self._log.info(f"subscribed {bar_type} (poll at close + "
                       f"{poll_lag(timeframe_of(bar_type))}s)")

    async def _unsubscribe_bars(self, command) -> None:
        task = self._poll_tasks.pop(command.bar_type, None)
        if task:
            task.cancel()

    def _seconds_to_next_close(self, timeframe: str) -> float:
        """Modular arithmetic on the UTC epoch, which is the right clock here.

        A GLBX "day" is a UTC calendar day — that is what `db_loader` buckets on and what
        `data/futures/1d` is grouped by, verified to 0.00 bp against the hourly bars — so
        the daily boundary really is midnight UTC and not a Chicago session edge.
        """
        step = db_live.INTERVALS[timeframe].total_seconds()
        return step - (datetime.now(timezone.utc).timestamp() % step)

    def _emit(self, bar_type: BarType, front: pd.DataFrame,
              behind: pd.DataFrame) -> pd.Timestamp | None:
        """Publish every bar newer than the last one, rolling the factor as it goes."""
        symbol = bar_type.instrument_id.symbol.value
        last = self._last_open.get(bar_type)
        fresh = front[front.index > last] if last is not None else front.tail(1)
        if fresh.empty:
            return None
        for ts, row in fresh.iterrows():
            new_id = int(row["instrument_id"]) if "instrument_id" in row else None
            if new_id is not None and new_id != self._contract.get(symbol):
                if self._contract.get(symbol) is None:
                    # No anchor yet — the warm-up never landed. Take this contract as the
                    # anchor rather than adjusting against nothing.
                    self._contract[symbol] = new_id
                    self._publish_factor(symbol, self._factor.get(symbol, 1.0))
                else:
                    self._apply_roll(symbol, front.loc[:ts], behind, new_id)
            factor = self._factor.get(symbol, 1.0)
            scaled = {c: float(row[c]) * factor
                      for c in ("Open", "High", "Low", "Close")}
            scaled["Volume"] = row.get("Volume")
            bar = self._to_bar(bar_type, ts, scaled)
            if bar is not None:
                self._handle_data(bar)
        return fresh.index[-1]

    async def _poll(self, bar_type: BarType) -> None:
        timeframe = timeframe_of(bar_type)
        symbol = bar_type.instrument_id.symbol.value
        while True:
            try:
                await asyncio.sleep(self._seconds_to_next_close(timeframe)
                                    + poll_lag(timeframe))
                for _ in range(MAX_RETRIES):
                    try:
                        front, behind = await asyncio.to_thread(
                            db_live.fetch_raw, symbol, timeframe, POLL_BARS)
                    except Exception as exc:        # noqa: BLE001 - retried, not fatal
                        self._log.warning(f"{bar_type} poll error: {exc}")
                        front = behind = pd.DataFrame()
                    if not front.empty:
                        newest = self._emit(bar_type, front, behind)
                        if newest is not None:
                            self._last_open[bar_type] = newest
                            break
                    await asyncio.sleep(RETRY_EVERY)
                else:
                    self._log.warning(
                        f"{bar_type}: no settled bar after "
                        f"{MAX_RETRIES * RETRY_EVERY}s — will wait for the next close")
            except asyncio.CancelledError:
                raise
            except Exception as exc:                # noqa: BLE001 - the loop must survive
                self._log.error(f"{bar_type} poll loop error: {exc}")
                await asyncio.sleep(RETRY_EVERY)


class DatabentoLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock) -> DatabentoLiveClient:
        return DatabentoLiveClient(loop=loop, msgbus=msgbus, cache=cache,
                                   clock=clock, config=config)
