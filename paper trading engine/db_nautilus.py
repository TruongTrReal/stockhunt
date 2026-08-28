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

Everything else is `td_nautilus`'s shape, deliberately: dedup on the bar's open time, a
briefly-cached warm-up shared across the strategies that ask for it at once, and the last
closed bar republished once so the sandbox exchange has a market to fill against.

**Two feeds, one publish path.** Ongoing bars come from Databento's LIVE gateway
(`db_stream.py`, measured 0.01s behind a bar's close) where the size allows it, and from
the historical REST archive (`_poll`, which waits for the archive's own frontier to pass
the bar — 3.5 to 13 minutes) where it does not or when the gateway is gone. The two differ only in how a raw frame is obtained: both hand
the same `(front, behind)` pair to `_emit`, so the roll arithmetic below has no branch in
it for which feed produced the bar, and `test_futures_leg.py`'s back-adjustment gates
cover both without knowing.

    1m, 1h        gateway, falling back to the poller
    1d            poller only -- `db_loader.merge_session_stubs` needs the NEXT session's
                  contract to decide about this one's Sunday stub, which is a batch
                  concept, and a daily bar gains nothing from arriving 18s after midnight
                  UTC instead of a quarter of an hour after it
    no SDK,       poller, loudly. `db_stream.have_sdk` answers instead of raising for the
    no key        same reason `db_live.have_key` does

Warm-up stays on the REST path in every case: a live gateway serves no history.

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
import db_stream
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

# Seconds after a bar boundary before the first fetch attempt — the FALLBACK, for when the
# archive cannot be asked where it has got to.
#
# **These were sized against a sample, and the sample was lucky.** The archive frontier does
# not trail real time smoothly: it advances in steps of roughly ten minutes, so a reading
# taken just after a step is small and one taken just before the next step is large.
# Re-sampled every ~28s on 2026-08-28 from 13:00:42 UTC, `metadata.get_dataset_range`'s
# top-level end ran **10.7 -> 13.0 minutes** behind and then dropped to **3.5** as the
# archive advanced. The earlier 5.5-7.3 minute reading was one sawtooth caught on its way
# up; 8 minutes covers the middle of that tooth and not its peak, and 15 minutes for the
# other sizes is a guess in the other direction.
#
# So the lag is no longer the thing that decides when to fetch — `_wait_for_frontier` asks
# the archive whether it holds the bar. Nothing broke while these were wrong (the retry
# loop below is 40 minutes of cover) but a poll inside the lag finds nothing settled and
# says nothing about it, which is a silence this class has paid for before.
POLL_LAG = 15 * 60

# **The per-timeframe split is gone, and that is a consequence of the fix rather than a
# separate decision.** `{"1m": 8 * 60}` existed because the lag DECIDED when a bar arrived,
# so a number sized for a daily bar cost fifteen bars at 1m. It no longer decides anything
# on the normal path, and on the fallback path a lag that does not clear the measured worst
# case (13 min) finds nothing settled and hands the whole job to the retry loop anyway. One
# number, and it is the one that clears the measurement.
POLL_LAG_BY_TF: dict[str, int] = {}


def poll_lag(timeframe: str) -> int:
    """Seconds after a bar boundary before the first fetch, when the frontier is unreadable.

    Only reached from `_wait_for_frontier`'s failure branch; the normal path waits for the
    archive to say it has the bar.
    """
    return POLL_LAG_BY_TF.get(timeframe, POLL_LAG)


# How the frontier wait behaves. All three are about bounding a wait, not about latency:
# the archive answers when it answers, and the only decisions here are how often to ask and
# when to stop asking.
#
# `FRONTIER_FLOOR` is the sleep before the FIRST question, so a poll task does not spend a
# request on an answer that is certainly "not yet" — the lowest reading ever taken on this
# dataset is 3.5 minutes and the floor sits well inside it.
# `FRONTIER_EVERY` is the ask cadence; `db_live.available_end` memoises for 60s, so half of
# these are free and the archive is asked at most once a minute per schema across every
# poll task on the desk.
# `FRONTIER_MAX_WAIT` is the ceiling, and it is deliberately SHORTER than the retry loop
# below (`MAX_RETRIES * RETRY_EVERY`, 40 minutes). A stalled archive must not wedge a poll
# task forever, and when this gives up the existing retry path takes over unchanged — the
# frontier wait is an optimisation on top of the backstop, never a replacement for it.
FRONTIER_FLOOR = 60
FRONTIER_EVERY = 30
FRONTIER_MAX_WAIT = 20 * 60
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
        # The LIVE gateway, or None when this process is polling. `_streamed` is the
        # reverse of what `db_stream` knows: it maps a streamed (symbol, timeframe) back to
        # the bar type to publish on, which is what `_emit` needs and what a fallback needs
        # in order to start a poll task for exactly the subscriptions the stream had.
        self._stream: db_stream.FuturesStream | None = None
        self._streamed: dict[tuple[str, str], BarType] = {}

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
            self._set_mode("poll", db_live.NO_KEY)
            return
        self._log.info(f"Databento {db_live.DATASET} connected, warmup window "
                       f"{self._window} bars, polls wait for the archive frontier "
                       f"(measured 3.5-13 min behind, ceiling "
                       f"{FRONTIER_MAX_WAIT // 60} min)")
        self._start_stream()

    def _start_stream(self) -> None:
        """Open the LIVE gateway, unless something says to poll instead.

        Never raises, and the reason is the same one `_connect` carries: this runs while a
        node is being built and the desk holds live positions on four other classes. Every
        way this can fail — no SDK, no gateway, a deliberate `STOCKHUNT_FUTURES_FEED=poll`
        — lands on the poller with a sentence, which is the behaviour that existed before
        the stream did.
        """
        wanted = db_stream.wanted_mode()
        if wanted == "poll":
            self._set_mode("poll", f"{db_stream.FEED_ENV}=poll — asked for, not fallen "
                                   f"back to")
            self._log.warning(
                f"CME futures leg polling the historical archive by request "
                f"({db_stream.FEED_ENV}=poll): bars will arrive about "
                f"{db_live.ARCHIVE_LAG_SECONDS // 60} minutes after they close.")
            return
        stream = db_stream.FuturesStream(self._on_stream_bars, self._on_stream_mode,
                                         self._log)
        try:
            started = stream.start()
        except Exception as exc:                # noqa: BLE001 - never fatal, see above
            self._log.error(f"the Databento live gateway would not start: {exc}")
            self._set_mode("poll", f"the Databento live gateway would not start: {exc}")
            return
        if not started:
            # `FuturesStream._degrade` has already named the reason through `_on_stream_mode`
            # — no SDK, or no key — and repeating it here would overwrite the specific
            # sentence with a vague one.
            return
        self._stream = stream
        # NOT `stream` yet: the session is opened by the supervisor on the first
        # subscription, and it is that open which reports the mode. Claiming an 18-second
        # feed before a socket exists is the silent-downgrade failure with the sign
        # flipped.
        self._set_mode("poll", "no futures subscription yet — the live gateway opens on "
                               "the first one")

    def _set_mode(self, mode: str, why: str) -> None:
        """Publish which feed this leg is on. See `db_live.FEED_MODE` for why it is
        published rather than inferred."""
        db_live.FEED_MODE.update(mode=mode, why=why)

    async def _disconnect(self) -> None:
        for task in self._poll_tasks.values():
            task.cancel()
        self._poll_tasks.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

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
            # `max`, not assignment, and the LIVE gateway is why. Warm-up comes from the
            # REST archive and is ~8 minutes stale by construction, while a streamed bar
            # can land within seconds of a subscription — so on a fast start this request
            # can complete AFTER a bar has already been published, and assigning here
            # would wind the watermark back and re-emit every bar in between. The poller
            # never raced it: its first fetch is a bar boundary plus fifteen minutes away.
            newest = df.index[-1]
            seen = self._last_open.get(bar_type)
            self._last_open[bar_type] = newest if seen is None else max(seen, newest)
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
        timeframe = timeframe_of(bar_type)
        symbol = bar_type.instrument_id.symbol.value
        # The gateway first, the timer second. `subscribe` answers False rather than
        # raising for every reason the stream cannot take this one — degraded, 1d, a
        # refused request — so the fallback below is the same line in all of them.
        if self._stream is not None and self._stream.subscribe(symbol, timeframe):
            self._streamed[(symbol, timeframe)] = bar_type
            self._log.info(f"subscribed {bar_type} on the Databento LIVE gateway "
                           f"(sub-second behind the close)")
            return
        self._poll_tasks[bar_type] = self.create_task(self._poll(bar_type))
        self._log.info(f"subscribed {bar_type} (polled: each bar is fetched once the "
                       f"archive frontier has passed it, ~"
                       f"{db_live.ARCHIVE_LAG_SECONDS // 60} min behind the close)")

    async def _unsubscribe_bars(self, command) -> None:
        task = self._poll_tasks.pop(command.bar_type, None)
        if task:
            task.cancel()

    # ------------------------------------------------------------------ the live gateway
    def _on_stream_bars(self, symbol: str, timeframe: str, front: pd.DataFrame,
                        behind: pd.DataFrame) -> None:
        """A streamed bar, handed to the poller's publish path unchanged.

        **Marshalled onto the node's event loop and not published from here.** This runs
        on the `databento` SDK's own reader thread, and `_handle_data` walks straight into
        the Nautilus message bus, which every other producer in this process reaches from
        the loop thread — `td_nautilus` and `_poll` both do their vendor work in
        `asyncio.to_thread` and publish from the coroutine. Publishing from a foreign
        thread is the kind of race that shows up as a corrupted book once a week and never
        in a test.
        """
        bar_type = self._streamed.get((symbol, timeframe))
        if bar_type is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._publish_streamed, bar_type, front,
                                            behind)
        except RuntimeError:
            # The loop is closed: the node is shutting down while a bar was in flight.
            # Not an error, and not something to log on every stop.
            pass

    def _publish_streamed(self, bar_type: BarType, front: pd.DataFrame,
                          behind: pd.DataFrame) -> None:
        """On the loop thread. `_emit` is the poller's, roll arithmetic and all."""
        try:
            newest = self._emit(bar_type, front, behind)
            if newest is not None:
                self._last_open[bar_type] = newest
        except Exception as exc:                # noqa: BLE001 - one bar, not the feed
            self._log.error(f"{bar_type}: could not publish a streamed bar: {exc}")

    def _on_stream_mode(self, mode: str, why: str) -> None:
        """The gateway gave up. Start a poll task for everything it was carrying.

        The leg must not go dark, and it must not go quietly slower either: `_set_mode`
        publishes the downgrade into `db_live.FEED_MODE`, which is what the desk's own
        state and `desk_control._caveat` read. Scheduled onto the loop because this
        arrives on the stream's supervisor thread and `create_task` is the node's.
        """
        self._set_mode(mode, why)
        if mode == "stream":
            return
        try:
            self._loop.call_soon_threadsafe(self._fall_back_to_polling)
        except RuntimeError:
            pass

    def _fall_back_to_polling(self) -> None:
        for (symbol, timeframe), bar_type in list(self._streamed.items()):
            if bar_type in self._poll_tasks:
                continue
            self._poll_tasks[bar_type] = self.create_task(self._poll(bar_type))
            self._log.warning(
                f"{bar_type}: now polled instead of streamed, so each bar waits for the "
                f"archive frontier to pass it. Its bars will arrive up to "
                f"{db_live.ARCHIVE_LAG_SECONDS // 60} minutes after they close.")
        self._streamed.clear()

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

    async def _wait_for_frontier(self, timeframe: str, bar_close: pd.Timestamp) -> str:
        """Sleep until the archive actually holds the bar that closed at `bar_close`.

        Returns what happened — `"arrived"`, `"timeout"` or `"unreadable"` — so the caller
        can say so in the log rather than leaving a wait indistinguishable from a stall.

        **The question is asked, not assumed.** `db_live.available_end(schema)` is the
        frontier floored to that schema's own bar boundary, so `end >= bar_close` is
        exactly "this bar is in the archive" for `1m`, `1h` and `1d` alike — measured
        2026-08-28: at 13:03:02 the `ohlcv-1h` end read 12:00 while the top-level frontier
        was 12:50, and at 13:03:31 both read 13:00.

        A frontier that cannot be read at all (no key, an HTTP error, a vendor schema
        change) falls back to `poll_lag` and the retry loop, which is what this desk did
        before and is still correct — just slower and blinder. **It must never raise**: a
        poll task that dies takes a leg's feed with it.
        """
        await asyncio.sleep(FRONTIER_FLOOR)
        schema = db_live.SCHEMA[timeframe]
        waited = FRONTIER_FLOOR
        while waited < FRONTIER_MAX_WAIT:
            try:
                end = await asyncio.to_thread(db_live.available_end, schema)
            except Exception as exc:            # noqa: BLE001 - degraded, never fatal
                self._log.warning(
                    f"{schema}: could not read the archive frontier ({exc}); falling back "
                    f"to a fixed {poll_lag(timeframe)}s lag for this bar")
                await asyncio.sleep(max(poll_lag(timeframe) - waited, 0))
                return "unreadable"
            if end >= bar_close:
                return "arrived"
            await asyncio.sleep(FRONTIER_EVERY)
            waited += FRONTIER_EVERY
        return "timeout"

    async def _poll(self, bar_type: BarType) -> None:
        timeframe = timeframe_of(bar_type)
        symbol = bar_type.instrument_id.symbol.value
        while True:
            try:
                await asyncio.sleep(self._seconds_to_next_close(timeframe))
                # Floored AFTER the sleep, not computed before it: the sleep lands just
                # past the boundary, so `now` floored to the interval IS the bar that has
                # just closed. Deriving it beforehand would be one interval out whenever
                # the loop woke a moment early.
                bar_close = pd.Timestamp.now(tz="UTC").tz_localize(None).floor(
                    db_live.INTERVALS[timeframe])
                how = await self._wait_for_frontier(timeframe, bar_close)
                if how == "timeout":
                    self._log.warning(
                        f"{bar_type}: the archive frontier had not reached {bar_close} "
                        f"after {FRONTIER_MAX_WAIT // 60} minutes — trying anyway, and the "
                        f"retry loop is the backstop")
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
