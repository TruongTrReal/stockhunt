"""Databento's LIVE gateway as a push feed for the CME futures leg.

`db_live.py` is the same vendor over REST; this is the same vendor over a socket, and the
split between the two files is the split the folder already has one vendor over —
`td_live` is the vendor and `td_nautilus` is the Nautilus client. Nothing here imports
`nautilus_trader`, so `db_live`, `desk_control` and the tests can read it without dragging
the trading stack in.

**Why this file exists at all.** `db_live`'s docstring says the historical archive IS the
live feed, and that was true and measured: the archive runs ~8 minutes behind, a poll
aligned to the bar close plus a lag reads the settled bar, and it costs $0.00. It is still
true for a book that decides once a day. It stopped being good enough when `1m` was opened
to member registrations on 2026-08-28: a minute bar reaching the desk eight minutes after
it closed means a member's fill is priced off a print eight bars old, which is what
`desk_control._caveat` had to say out loud on every 1m futures registration.

Measured 2026-08-28 on this key, side by side:

    poller (historical REST)   ~8 min   behind the bar's close
    this module (live gateway)   0.01 s behind the bar's close

and `metadata.get_cost(..., mode="live")` for `ES.v.0` at `ohlcv-1m` prices at **$0.00**,
the same as historical — `metadata.list_unit_prices` carries a `live` block for
`GLBX.MDP3`, so the mode exists on this account. The reason there was no live feed before
was not cost and not capability: the repo spoke to Databento with `requests` and the
`databento` SDK was never a dependency. Nobody had written one.

**That 0.01s is measured in the GATEWAY's clock, and the distinction is not pedantic.**
Subscribing with `ts_out=True` stamps every record with the instant Databento sent it, so
`ts_out - (ts_event + interval)` contains no local clock at all: 12 bars over three roots,
min 0.00s, max 0.01s. The obvious measurement — `time.time() - ts_event` — reads **18.1
seconds** instead, consistently, and it is wrong twice over. `ts_event` is a bar's OPEN and
not its close, and **this development box's clock runs 42.01s behind the gateway's** (mean
over 22 heartbeats, whose `ts_event` is server time). 60 - 42.01 = 17.99, which is the
whole of the 18.1.

That skew is worth knowing about beyond this file: everything on this box that reasons
about "now" against a vendor timestamp — `db_live._drop_forming`,
`db_nautilus._seconds_to_next_close`, the decide-early cutoff — carries those 42 seconds.
Re-measure latency against `ts_out` or a heartbeat, never against the wall clock. This
module needs no wall clock at all for correctness, which is one more thing a push feed is
better at than a timer.

**The gateway delivers RAW continuous prices, exactly as the poller's raw fetch does**, so
every streamed bar has to go through the same ratio back-adjustment. That is not repeated
here. This module's whole output contract is a pair of frames in `db_live.fetch_raw`'s
shape — rank 0 and rank 1, `Open/High/Low/Close/Volume/instrument_id` on a tz-naive UTC
index — which is what `DatabentoLiveClient._emit` already takes. Streamed bars therefore
reach a strategy through the *identical* roll arithmetic the polled ones do, and
`test_futures_leg.py`'s back-adjustment gates cover both without knowing which fed them.

Four properties are the whole difficulty of a stream, and each is a decision here:

* **A bar that never arrives.** The gateway sends an OHLCV record only for an interval the
  instrument actually traded in, so silence on `LE.v.0` overnight is correct and silence
  on the whole session is a dead socket. Liveness is therefore measured on the SESSION —
  gateway heartbeats, which arrive every `HEARTBEAT_SECONDS` whether anything trades or
  not — and never on a symbol's bars. Reading it off bars is the fifteen-hour silent
  failure `td_nautilus.timeframe_of` is named for, wearing a socket.
* **A reconnect that loses the gap.** The SDK's own `ReconnectPolicy.RECONNECT`
  re-subscribes with `start=None` — verified by reading `databento.live.session._reconnect`
  — so it resumes live and the bars during the outage are simply gone. So the policy is
  deliberately `NONE` and the reconnect is owned here: a fresh session subscribed with
  `start = <last bar seen>`, which the gateway replays and then transitions seamlessly to
  live. Measured: a 20-minute replay delivered in ~5s and ran on into live bars with no
  hole and no duplicate.
* **A first connect that leaves a hole.** Warm-up comes from the REST archive and is ~8
  minutes stale by construction, so a stream that started at "now" would skip the bars in
  between. The same replay mechanism closes it: the first subscribe asks for
  `catchup_start`, and `_emit`'s own `_last_open` filter drops whatever the warm-up
  already published. One mechanism, two uses.
* **A gateway that will not come back.** After `MAX_ATTEMPTS` consecutive failed
  reconnects the stream declares itself degraded and hands the leg back to the poller,
  loudly. A silent downgrade to an eight-minute feed is worse than either mode chosen
  deliberately.

**1d is deliberately not streamed.** Two reasons and both are structural: a daily bar
gains nothing from arriving a hundredth of a second after midnight UTC instead of eight
minutes after it, and `db_loader.merge_session_stubs` — which folds Sunday's two-hour
opening sliver into the session it opens, or drops it when the weekend carried the roll —
needs to see the NEXT session's contract before it can decide about this one. That is a
batch concept, and a stream has no next bar yet: it would publish a Sunday stub as a day,
and `IBS` is `(C-L)/(H-L)`: it reads exactly that geometry.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pandas as pd

import paper_config                       # noqa: F401  (puts `backtest engine/` on the path)

import db_live
import db_loader

DATASET = db_live.DATASET

# The `databento` SDK is imported lazily and its absence is ANSWERED, not raised.
#
# It was never a dependency of this repo — every Databento call before this file went out
# over `requests` — so a box that has not been re-provisioned since (the VPS, on
# 2026-08-28) has `db_loader` working and no SDK. That must degrade the futures leg to the
# poller with a sentence in the log, never take down a desk holding live positions on four
# other classes. Same reasoning as `db_live.have_key`, one dependency over.
NO_SDK = ("the `databento` SDK is not installed, so the CME futures leg cannot use the "
          "LIVE gateway and will fall back to polling the historical archive (~8 minutes "
          "behind). `pip install databento` in the desk's venv")

# What a caller sets to choose the feed deliberately. `stream` is the default; `poll` is
# the old behaviour, kept because a deliberate choice of the slower feed is a legitimate
# thing to want (comparing the two, or working around a vendor incident) and because a
# switch is how a suspicion gets tested without an edit.
FEED_ENV = "STOCKHUNT_FUTURES_FEED"

# Which timeframes the gateway feeds. `1d` is excluded on purpose — see the module
# docstring's last paragraph, which is about `merge_session_stubs`, not about latency.
STREAM_TIMEFRAMES = ("1h", "1m")

# DBN carries prices as a signed integer of 1e-9 units, and the LIVE path is where that
# matters: `db_loader._get_range` passes `pretty_px=true` so the REST CSV already holds
# decimals, while a record off the socket does not. Verified against the vendor on
# 2026-08-28 — `rec.close` of 7740500000000 is ES at 7740.50, matching `rec.pretty_close`
# and the archive's own bar for the same minute. Written as a divisor rather than taken
# from `pretty_close` because the fixed-point scale is the DBN wire format and cannot
# drift, while a convenience accessor can change what it returns.
PRICE_SCALE = 1e9

# How many bars of each (symbol, timeframe) to keep. It only has to be deep enough that a
# roll's own bar and the bar before it are both present, which is what makes the same-bar
# rank-1 ratio measurable — the same reason `db_nautilus.POLL_BARS` is 8 rather than 3.
# Deeper than that costs nothing and buys room for a replay to land beside live bars.
BUFFER_BARS = 64

# The gateway sends a heartbeat on this cadence when nothing else is flowing, which is
# what makes silence measurable on an illiquid root's overnight session. Ten seconds
# matches what the vendor was observed to honour on 2026-08-28.
HEARTBEAT_SECONDS = 10

# Silence longer than this and the session is treated as dead. Six missed heartbeats: long
# enough that a slow moment is not a reconnect, short enough that a wedged socket costs
# one bar at 1m rather than a session.
SILENCE_SECONDS = 6 * HEARTBEAT_SECONDS

# How long to wait after the newest buffered bar before flushing it anyway.
#
# The normal flush trigger is the gateway's own `End of interval` system message, which
# arrives once every symbol's bar for that interval has been sent — so a roll ratio is
# computed with rank 0 and rank 1 both present rather than against a half-filled buffer.
# This is the backstop for a session that stops sending that message: two seconds of extra
# latency on a path that should never run, against a bar that otherwise sits in a buffer
# forever.
FLUSH_GRACE_SECONDS = 2.0

# Reconnect backoff, in seconds, one entry per attempt. It ends rather than repeating: an
# exhausted ladder is the signal to degrade to the poller, and a ladder that retried
# forever would leave the leg silently dark for as long as the outage lasted.
BACKOFF = (1, 2, 5, 15, 30, 60)
MAX_ATTEMPTS = len(BACKOFF)

# How long the wanted set must stop growing before the session is (re)built for it.
#
# Nineteen roots at two sizes arrive as one burst when the node starts, and each one would
# otherwise be a socket: `Live.subscribe` cannot carry a replay after `Live.start()`, so a
# late subscription is a new session or a hole, and the cheapest way to make it one
# session is to wait for the burst to finish. Three seconds is longer than the burst and
# shorter than a bar at every size streamed here.
SUBSCRIBE_DEBOUNCE_SECONDS = 3.0

# What the gateway says when the key already has as many live sessions open as it is
# allowed. Matched as a substring rather than on an exception type because the SDK raises
# one `BentoError` for every gateway refusal and only the text distinguishes them.
#
# It is not hypothetical: it fired on 2026-08-28 while two verification runs were still
# holding sockets, and the reconnect ladder backed off 1s, 2s, 5s, 15s, 30s and connected
# on the fifth attempt. Two things follow — the desk must not leave stray sessions behind
# (`FuturesStream.stop` terminates), and a REBUILD has to be able to give up its overlap.
# See `_open`.
CONNECTION_LIMIT = "open connection limit"

# How far back the first subscribe replays, per timeframe.
#
# It has to clear `db_live.ARCHIVE_LAG_SECONDS`, because that is exactly the hole between
# the newest bar a REST warm-up can return and the first bar a live session would send.
# Capped because the gateway's intraday replay reaches back to the start of the current
# session and no further; asking for more is a refusal, which the ladder in `_open`
# answers by retrying without a start.
CATCHUP_CAP_SECONDS = 6 * 3600


def catchup_seconds(timeframe: str) -> int:
    """How far back a fresh subscription replays before going live.

    Four bars or twice the archive lag, whichever is larger, capped at the session. The
    archive-lag term is what closes the warm-up hole at 1m (eight bars of it); the
    four-bar term is what keeps the hourly subscription from replaying a token amount.
    """
    step = db_live.INTERVALS[timeframe].total_seconds()
    return int(min(max(4 * step, 2 * db_live.ARCHIVE_LAG_SECONDS), CATCHUP_CAP_SECONDS))


def have_sdk() -> bool:
    """Is the `databento` SDK importable — asked WITHOUT raising. See `NO_SDK`."""
    try:
        import databento                      # noqa: F401
        return True
    except Exception:                          # noqa: BLE001 - absence is the answer here
        return False


def can_stream(timeframe: str) -> bool:
    """Whether this size is fed by the gateway rather than by the poller.

    Both halves bind. A timeframe the vendor has no schema for is refused by
    `db_live.can_feed` at subscribe time already; this is the narrower question of whether
    the LIVE path handles it, and `1d` answers no while being perfectly feedable.
    """
    return timeframe in STREAM_TIMEFRAMES and db_live.can_feed(timeframe)


def wanted_mode(env: dict | None = None) -> str:
    """`stream` or `poll`, from `STOCKHUNT_FUTURES_FEED`. Anything else is `stream`.

    An unrecognised value reads as the default rather than raising, because this is
    consulted while a node is being built and a typo in a systemd unit must not be the
    thing that stops a desk starting. `--futures-feed` on `run_paper.py` is the reviewable
    way to set it.
    """
    import os
    src = os.environ if env is None else env
    return "poll" if str(src.get(FEED_ENV, "")).strip().lower() == "poll" else "stream"


def _empty() -> pd.DataFrame:
    """The frame shape `db_live.fetch_raw` returns, with no rows in it."""
    frame = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume",
                                  "instrument_id"])
    frame.index = pd.DatetimeIndex([], name="Date")
    return frame


class _Buffer:
    """The last `BUFFER_BARS` bars of one (symbol, timeframe), as rows keyed by open time.

    A dict rather than a DataFrame because every record appends one row and rebuilding a
    frame per bar would be the dominant cost of the callback thread — the frame is built
    once per flush instead.
    """

    def __init__(self) -> None:
        self.rows: dict[pd.Timestamp, dict] = {}

    def put(self, ts: pd.Timestamp, row: dict) -> None:
        self.rows[ts] = row
        if len(self.rows) > BUFFER_BARS:
            for old in sorted(self.rows)[:-BUFFER_BARS]:
                del self.rows[old]

    def frame(self) -> pd.DataFrame:
        if not self.rows:
            return _empty()
        keys = sorted(self.rows)
        frame = pd.DataFrame([self.rows[k] for k in keys],
                             index=pd.DatetimeIndex(keys, name="Date"))
        frame["instrument_id"] = frame["instrument_id"].astype("int64")
        return frame

    def newest(self) -> pd.Timestamp | None:
        return max(self.rows) if self.rows else None


class FuturesStream:
    """One Databento Live session for the whole CME leg, supervised.

    One session and not one per subscription: a session is a socket and a login, the
    gateway multiplexes schemas onto it (an `OHLCVMsg`'s `rtype` says which), and nineteen
    roots at two sizes would otherwise be thirty-eight connections to keep alive. The cost
    of the single session is that a reconnect re-subscribes everything, which is why every
    subscription is remembered in `_wanted` rather than only sent.

    **Every call into the gateway happens on this object's own supervisor thread**, and
    that is not tidiness. `Live.subscribe` opens and authenticates the socket on its first
    call, so it blocks; the callers are `DatabentoLiveClient._connect` and
    `_subscribe_bars`, which are coroutines on the node's event loop, and a blocking round
    trip there stalls every other client on the desk. `subscribe()` here therefore only
    records a want, and the supervisor opens the session.

    **A subscription added after `start()` cannot ask for a replay** — the SDK says so and
    the gateway enforces it — so a symbol registered later would have a hole between its
    REST warm-up (~8 minutes stale) and its first live bar. The supervisor answers that by
    rebuilding the session whenever the wanted set grows, replaying from where the last
    one left off. Rebuilds are debounced by `SUBSCRIBE_DEBOUNCE_SECONDS`, because the
    desk's subscriptions arrive as one burst at start-up and rebuilding once per strategy
    would be nineteen sockets in a second.

    Callbacks:

        on_bars(symbol, timeframe, front, behind)   two frames in `fetch_raw`'s shape
        on_mode(mode, why)                          "stream" | "poll", with a sentence

    `on_bars` arrives on the SDK's reader thread and lands in
    `DatabentoLiveClient._emit`, the poller's publish path unchanged; `on_mode` arrives on
    the supervisor thread. Both callers marshal onto the node's loop themselves.
    """

    def __init__(self, on_bars, on_mode, log) -> None:
        self._on_bars = on_bars
        self._on_mode = on_mode
        self._log = log
        # (symbol, timeframe) the desk wants. Rank 1 is derived, never registered — see
        # `_symbols_for`.
        self._wanted: set[tuple[str, str]] = set()
        # What the CURRENT session was actually opened with. `_wanted` growing past this
        # is what makes the supervisor rebuild, and comparing the two is the only way to
        # tell "asked for and not yet on the wire" from "on the wire and silent".
        self._live_set: set[tuple[str, str]] = set()
        self._wanted_changed_at = 0.0
        self._buffers: dict[tuple[str, str], _Buffer] = {}
        # `stype_in_symbol` of the mapping that is CURRENT for each instrument id, and the
        # forward map it is derived from. See `_remap` for why both exist.
        self._iid_of: dict[str, int] = {}
        self._symbol_of: dict[int, str] = {}
        self._flushed: dict[tuple[str, str], pd.Timestamp] = {}
        self._pending: dict[tuple[str, str], float] = {}
        self._client = None
        self._lock = threading.RLock()
        self._last_record = 0.0
        # The gateway's clock, off its heartbeats. See `_record`.
        self._gateway_ns = 0
        # How late the newest bar was, in the GATEWAY's clock. See `latency_seconds`.
        self._latency_s: float | None = None
        self._degraded = False
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._attempt = 0
        # The newest bar delivered on ANY subscription, which is where a reconnect asks the
        # gateway to replay from. Per (symbol, timeframe) would be more precise and is
        # worse: the gateway takes one `start` per subscription request, and replaying a
        # little too much is free — `_emit` drops what it has already published.
        self._last_bar: dict[str, pd.Timestamp] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start supervising. False means the caller must poll instead, permanently.

        It does NOT open a session, because there is nothing to subscribe to yet — the
        SDK refuses `Live.start()` before a subscription — and because the two things that
        can be answered here without a socket are the two that decide whether a stream is
        possible at all. Both are answered rather than raised, for the reason
        `db_live.have_key` is: this runs while a node is being built and the desk holds
        live positions on four other classes.
        """
        if not have_sdk():
            self._degrade(NO_SDK)
            return False
        if not db_live.have_key():
            self._degrade(db_live.NO_KEY)
            return False
        self._supervisor = threading.Thread(target=self._supervise, name="db-stream",
                                            daemon=True)
        self._supervisor.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.terminate()
            except Exception:                  # noqa: BLE001 - shutting down anyway
                pass

    @property
    def degraded(self) -> bool:
        return self._degraded

    def mode(self) -> str:
        return "poll" if self._degraded else "stream"

    # ------------------------------------------------------------------ subscription
    def subscribe(self, symbol: str, timeframe: str) -> bool:
        """Record that the desk wants one root at one size. False means: poll it instead.

        It returns before the gateway has been told, and it has to: this is called from a
        Nautilus coroutine and the gateway call blocks. What it promises is that the
        supervisor will carry this subscription for as long as the stream is up, and that
        if the stream ever gives up, `on_mode("poll", ...)` fires so the caller can start
        a timer for everything it was carrying. A per-subscription failure is not a thing
        here — a session either serves the whole leg or the whole leg polls, which is also
        the only version a reader of the log can act on.

        Idempotent: a bar type subscribed twice — which happens whenever two strategies on
        one instrument start together — changes nothing the second time and forces no
        rebuild.
        """
        if self._degraded or not can_stream(timeframe):
            return False
        key = (symbol, timeframe)
        with self._lock:
            if key not in self._wanted:
                self._wanted.add(key)
                self._wanted_changed_at = time.monotonic()
        return True

    def _symbols_for(self, key: tuple[str, str]) -> list[str]:
        """Rank 0 and rank 1, for the same reason `db_live.fetch_raw` asks for both.

        The exact roll ratio is the incoming contract's price read on the SAME bar as the
        outgoing one, so rank 1 has to be on the wire before the roll happens, not fetched
        after it. A close-to-close splice is the fallback and folds a session of market
        movement into a live position's entry.
        """
        return [key[0], db_live.rank_one(key[0])]

    def _replay_starts(self, timeframe: str) -> list[pd.Timestamp | None]:
        """The replay ladder for one timeframe, most complete first.

        Three rungs, and each one is a different honest answer to "how much of the gap can
        this session recover":

        1. **from the last bar seen**, which is the whole gap and the point of owning the
           reconnect at all;
        2. **from `catchup_seconds` ago**, for when rung 1 reaches further back than the
           gateway's intraday replay window and is refused outright — a desk that was down
           overnight still wants the last quarter of an hour;
        3. **live only**, which leaves a hole, and says so.

        Rung 2 is not redundant with rung 1 on a fresh start either: it is what closes the
        gap between a REST warm-up, which is `ARCHIVE_LAG_SECONDS` stale by construction,
        and a socket that would otherwise begin at "now".
        """
        floor = pd.Timestamp.now(tz="UTC") - pd.Timedelta(
            seconds=catchup_seconds(timeframe))
        since = self._last_bar.get(timeframe)
        rungs: list[pd.Timestamp | None] = []
        if since is not None:
            earliest = min(since.tz_localize("UTC"), floor)
            rungs.append(earliest)
        if not rungs or rungs[0] != floor:
            rungs.append(floor)
        rungs.append(None)
        return rungs

    def _send(self, client, keys, catchup: bool) -> None:
        """One subscription request per timeframe, symbols batched into it.

        Batched because a subscription request is a round trip and the leg is nineteen
        roots at two ranks; the gateway takes a symbol list and one `start` per request,
        which is also why the replay ladder is per timeframe rather than per symbol.
        """
        by_tf: dict[str, list[str]] = {}
        for key in keys:
            by_tf.setdefault(key[1], []).extend(self._symbols_for(key))
        for timeframe, symbols in by_tf.items():
            schema = db_live.SCHEMA[timeframe]
            names = sorted(set(symbols))
            rungs = self._replay_starts(timeframe) if catchup else [None]
            for i, start in enumerate(rungs):
                try:
                    client.subscribe(dataset=DATASET, schema=schema,
                                     stype_in="continuous", symbols=names,
                                     **({"start": start} if start is not None else {}))
                    if start is None and catchup:
                        self._log.warning(
                            f"{schema}: subscribed LIVE-ONLY after the gateway refused "
                            f"every replay window. Bars before now are a HOLE in the "
                            f"buffer, not a silence — the next warm-up request is what "
                            f"repairs it.")
                    elif i:
                        self._log.warning(f"{schema}: replaying from {start} after the "
                                          f"gateway refused a longer window")
                    break
                except Exception as exc:       # noqa: BLE001 - the ladder answers it
                    if i == len(rungs) - 1:
                        raise
                    self._log.warning(f"{schema}: the gateway refused a replay from "
                                      f"{start}: {exc}")

    # ------------------------------------------------------------------ the session
    def _open(self) -> bool:
        """Build a session, subscribe everything wanted, start it. False on failure.

        Always on the supervisor thread, and always with a replay: this is the ONE way a
        session comes into existence here, whether it is the first one, a rebuild because
        a symbol was added, or a reconnect after silence. One path means the gap handling
        cannot be right in two of the three and wrong in the other.
        """
        import databento

        with self._lock:
            keys = sorted(self._wanted)
        if not keys:
            return False                       # nothing to subscribe; the SDK refuses
        old = self._client
        try:
            client = databento.Live(key=db_loader.api_key(),
                                    heartbeat_interval_s=HEARTBEAT_SECONDS,
                                    # Eight bytes a record, and it buys the only latency
                                    # measurement that has no local clock in it. See
                                    # `latency_seconds`.
                                    ts_out=True,
                                    # NONE on purpose. The SDK's own reconnect
                                    # re-subscribes with `start=None` — read
                                    # `databento.live.session._reconnect` — so it resumes
                                    # live and the outage's bars are simply gone. Owning
                                    # the reconnect is what lets it replay the gap.
                                    reconnect_policy="none")
            client.add_callback(self._record, self._callback_error)
            self._send(client, keys, catchup=True)
            client.start()
        except Exception as exc:               # noqa: BLE001 - answered, never raised
            self._log.error(f"Databento live session failed to open: "
                            f"{type(exc).__name__}: {exc}")
            if old is not None and CONNECTION_LIMIT in str(exc):
                # Measured against the real gateway on 2026-08-28: Databento caps
                # CONCURRENT live connections per key, and a rebuild is deliberately
                # overlapping — the old socket is held until the new one is carrying, so
                # the changeover costs a duplicate bar rather than a hole. When the cap is
                # what refused the new session, that ordering is the problem rather than
                # the protection, so it is inverted for one attempt: drop the old socket
                # first and accept the hole, because a leg with no session at all is worse
                # than a leg missing a bar.
                self._log.warning(
                    "the Databento live connection cap refused the overlapping rebuild — "
                    "dropping the current session first and retrying, which leaves a "
                    "short HOLE where the overlap would have left a duplicate")
                try:
                    old.terminate()
                except Exception:              # noqa: BLE001 - it is being discarded
                    pass
                with self._lock:
                    self._client = None
                    self._live_set = set()
                return self._open()
            return False
        with self._lock:
            self._client = client
            self._live_set = set(keys)
        # The old socket is dropped only once the new one is carrying, so a rebuild costs
        # duplicate bars for a moment rather than a hole. `_emit` filters a bar it has
        # already published on its open time; it cannot recover one that never arrived.
        if old is not None:
            try:
                old.terminate()
            except Exception:                  # noqa: BLE001 - it is being discarded
                pass
        self._last_record = time.monotonic()
        self._attempt = 0
        self._log.info(
            f"Databento LIVE gateway connected ({DATASET}, "
            f"{len(keys)} subscription(s) over "
            f"{'/'.join(sorted({db_live.SCHEMA[k[1]] for k in keys}))}); measured 0.01s "
            f"behind the bar close against ~{db_live.ARCHIVE_LAG_SECONDS // 60} min for "
            f"the historical poller")
        self._on_mode("stream", f"Databento {DATASET} live gateway")
        return True

    def _callback_error(self, exc: BaseException) -> None:
        """An exception raised INSIDE the record callback. Logged, never propagated.

        The SDK would otherwise let it escape into its own reader task, where the session
        is torn down for a reason that never reaches this log — a stream that stops for a
        bug in the handler and looks exactly like a stream that stopped for the vendor.
        """
        self._log.error(f"Databento live callback error: {type(exc).__name__}: {exc}")

    def latency_seconds(self) -> float | None:
        """How long after its interval closed the gateway SENT the newest bar.

        `ts_out - (ts_event + interval)`, both stamped by Databento, so the number is free
        of the local clock entirely — which matters here more than anywhere else in the
        repo, because this box's clock is 42 seconds behind the gateway's and the obvious
        `now - ts_event` reads a plausible, wrong 18.1s because of it.

        None until the first bar. It is the honest health signal for this feed: unlike a
        bar count it does not go stale when an illiquid root simply has nothing to print.
        """
        return self._latency_s

    def gateway_now(self) -> pd.Timestamp | None:
        """What time the GATEWAY thinks it is, from its last heartbeat.

        Exposed so a latency question can be answered without the local wall clock in it.
        This box's is 42 seconds behind Databento's, which is enough to make a freshly
        closed bar look either stale or impossible depending on which way you subtract.
        """
        return pd.Timestamp(self._gateway_ns, unit="ns") if self._gateway_ns else None

    def _record(self, rec) -> None:
        """Every record off the socket. Runs on the SDK's thread."""
        self._last_record = time.monotonic()
        try:
            name = type(rec).__name__
            if name == "SymbolMappingMsg":
                self._remap(rec)
            elif name == "OHLCVMsg":
                self._bar(rec)
            elif name == "ErrorMsg":
                self._log.error(f"Databento live gateway: {getattr(rec, 'err', rec)}")
            elif name == "SystemMsg":
                msg = str(getattr(rec, "msg", ""))
                if "Heartbeat" in msg:
                    # The gateway's own clock, and ONLY off a heartbeat. Kept because this
                    # box's is 42 seconds behind it (see the module docstring), so any
                    # "how old is this bar" answered against the wall clock inherits that
                    # error silently.
                    #
                    # Not off any system message: `End of interval`'s `ts_event` is the
                    # interval's START, not the instant it was sent, so taking it here
                    # made every bar look exactly one interval old — a plausible number,
                    # arrived at by reading a different quantity, which is the worst kind
                    # of wrong for a measurement.
                    self._gateway_ns = int(getattr(rec, "ts_event", 0)) or self._gateway_ns
                if msg.startswith("End of interval"):
                    self._flush_all()
                elif "Heartbeat" not in msg:
                    self._log.info(f"Databento live gateway: {msg}")
        except Exception as exc:               # noqa: BLE001 - one bad record, not a feed
            self._log.error(f"could not handle a Databento live record: "
                            f"{type(exc).__name__}: {exc}")

    def _remap(self, rec) -> None:
        """Follow which contract each continuous symbol currently names.

        This is how a ROLL reaches the desk on the stream: the gateway re-points `ES.v.0`
        at a new instrument id, and the bars that follow carry it. It is the same signal
        the poller reads off the `instrument_id` column, arriving as a message instead of
        as a column.

        The reverse map is rebuilt rather than assigned, and that is the whole reason both
        maps exist. At a roll `ES.v.0` and `ES.v.1` name the SAME contract for as long as
        it takes the second mapping message to arrive — rank 1's old front becomes rank
        0's new one — and a naive `reverse[iid] = symbol` would leave that bar filed under
        whichever message came last. Rebuilding with rank 0 winning files it under the
        front, which is what it now is, and leaves rank 1 without a bar for that interval,
        which is correct: the ratio `db_loader.back_adjust` needs is read off the bar
        BEFORE the roll, where the two were still different contracts.
        """
        symbol = str(rec.stype_in_symbol)
        iid = int(rec.instrument_id)
        with self._lock:
            self._iid_of[symbol] = iid
            reverse: dict[int, str] = {}
            for name in sorted(self._iid_of, reverse=True):
                reverse[self._iid_of[name]] = name
            self._symbol_of = reverse

    def _bar(self, rec) -> None:
        """One completed interval for one contract.

        **No `drop_forming` here, and that is a property of the gateway rather than an
        omission.** `db_live._drop_forming` exists because a REST window can return the
        bar that is still open, whose close has not happened yet; the live gateway emits an
        interval only once it has ended, and marks the batch with `End of interval`. The
        look-ahead the REST path has to defend against cannot occur on this one.
        """
        iid = int(rec.instrument_id)
        with self._lock:
            symbol = self._symbol_of.get(iid)
        if symbol is None:
            return                             # a mapping not yet seen; the next one lands
        timeframe = _TIMEFRAME_OF_RTYPE.get(int(rec.rtype))
        if timeframe is None:
            return
        ts = pd.Timestamp(int(rec.ts_event), unit="ns")
        # The one latency reading with no local clock in it — the gateway's own send
        # stamp minus the interval's close, both stamped by Databento. See
        # `latency_seconds`. Guarded on presence because `ts_out` is a subscription
        # option, and a session opened without it must still deliver bars.
        sent = int(getattr(rec, "ts_out", 0) or 0)
        if sent:
            close_ns = int(rec.ts_event) + int(
                db_live.INTERVALS[timeframe].total_seconds() * 1e9)
            self._latency_s = (sent - close_ns) / 1e9
        row = {
            "Open": rec.open / PRICE_SCALE,
            "High": rec.high / PRICE_SCALE,
            "Low": rec.low / PRICE_SCALE,
            "Close": rec.close / PRICE_SCALE,
            # float64 like every other class, so the volume-consuming TA-Lib rules see one
            # dtype everywhere. `db_loader._frame` does the same to the REST column.
            "Volume": float(rec.volume),
            "instrument_id": iid,
        }
        with self._lock:
            self._buffers.setdefault((symbol, timeframe), _Buffer()).put(ts, row)
            self._last_bar[timeframe] = max(
                ts, self._last_bar.get(timeframe, ts))
            root = _root_of(symbol)
            if (root, timeframe) in self._wanted:
                self._pending.setdefault((root, timeframe), time.monotonic())

    # ------------------------------------------------------------------ publishing
    def _flush_all(self) -> None:
        with self._lock:
            keys = list(self._pending)
        for key in keys:
            self._flush(key)

    def _flush(self, key: tuple[str, str]) -> None:
        """Hand one root's buffers to the client, once per interval.

        Both ranks in one call, in `db_live.fetch_raw`'s shape, because that is the pair
        `db_loader.back_adjust` prices a roll from. Everything after this point — the roll
        detection, the forward factor, the emitted Nautilus bar — is the poller's code
        with no branch in it for where the frame came from.
        """
        symbol, timeframe = key
        behind_symbol = db_live.rank_one(symbol)
        with self._lock:
            self._pending.pop(key, None)
            front_buf = self._buffers.get((symbol, timeframe))
            behind_buf = self._buffers.get((behind_symbol, timeframe))
            if front_buf is None:
                return
            newest = front_buf.newest()
            if newest is None or newest == self._flushed.get(key):
                return
            self._flushed[key] = newest
            front = front_buf.frame()
            behind = behind_buf.frame() if behind_buf is not None else _empty()
        try:
            self._on_bars(symbol, timeframe, front, behind)
        except Exception as exc:               # noqa: BLE001 - one bar, not the feed
            self._log.error(f"{symbol} {timeframe}: could not publish a streamed bar: "
                            f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ supervision
    def _supervise(self) -> None:
        """Watch the SESSION, not the bars. See the module docstring's first bullet."""
        while not self._stop.wait(1.0):
            try:
                self._sweep()
            except Exception as exc:           # noqa: BLE001 - the loop must survive
                self._log.error(f"Databento live supervisor error: "
                                f"{type(exc).__name__}: {exc}")

    def _sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [k for k, at in self._pending.items()
                     if now - at > FLUSH_GRACE_SECONDS]
        for key in stale:
            self._flush(key)
        if self._degraded:
            return
        with self._lock:
            grown = self._wanted - self._live_set
            settled = now - self._wanted_changed_at > SUBSCRIBE_DEBOUNCE_SECONDS
            client = self._client
        if grown and settled:
            # A subscription added after `Live.start()` cannot carry a replay, so it is a
            # NEW SESSION or a hole. Debounced, because the desk's subscriptions arrive as
            # one burst when the node starts and rebuilding per strategy would be nineteen
            # sockets in a second.
            self._log.info(f"rebuilding the Databento live session for "
                           f"{len(grown)} new subscription(s): "
                           f"{', '.join(f'{s} {t}' for s, t in sorted(grown))}")
            if not self._open():
                self._reconnect()
            return
        if client is None:
            return
        if now - self._last_record < SILENCE_SECONDS:
            return
        self._log.warning(
            f"no record from the Databento live gateway for "
            f"{now - self._last_record:.0f}s (heartbeat is every {HEARTBEAT_SECONDS}s) — "
            f"treating the session as dead and reconnecting")
        self._reconnect()

    def _reconnect(self) -> None:
        """Tear the session down and replay the gap into a fresh one.

        `terminate()` rather than `stop()`: `stop` waits for the reader to drain, and the
        socket being drained is the thing that has stopped responding.

        The ladder ENDS rather than repeating. Retrying forever would leave the leg dark
        for as long as the outage lasted, with a log line every minute saying so and
        nothing acting on it; exhausting it hands the leg back to the poller, which is
        slow and works.
        """
        with self._lock:
            client, self._client = self._client, None
            self._live_set = set()
        if client is not None:
            try:
                client.terminate()
            except Exception:                  # noqa: BLE001 - it is already broken
                pass
        while not self._stop.is_set() and self._attempt < MAX_ATTEMPTS:
            delay = BACKOFF[self._attempt]
            self._attempt += 1
            self._log.info(f"reconnecting the Databento live gateway in {delay}s "
                           f"(attempt {self._attempt} of {MAX_ATTEMPTS}); the gap since "
                           f"{self._last_bar or 'the warm-up'} will be replayed")
            if self._stop.wait(delay):
                return
            if self._open():
                return
        self._degrade(f"the Databento live gateway did not come back after "
                      f"{MAX_ATTEMPTS} attempts")

    def _degrade(self, why: str) -> None:
        """Hand the leg back to the poller, once, loudly.

        **It does not flap back.** The reconnect ladder above is what a transient outage
        costs; reaching here means the ladder was exhausted, and a feed that alternated
        between an 18-second and an eight-minute latency would put two different meanings
        under one published number with nothing to say which bar had which. A restart is
        the recovery, and the log and `db_live.FEED_MODE` both say so.
        """
        if self._degraded:
            return
        self._degraded = True
        self._log.error(f"CME futures leg falling back to the historical poller: {why}. "
                        f"Bars will arrive about "
                        f"{db_live.ARCHIVE_LAG_SECONDS // 60} minutes after they close "
                        f"instead of within a second of it.")
        try:
            self._on_mode("poll", why)
        except Exception as exc:               # noqa: BLE001 - reporting, never fatal
            self._log.error(f"could not report the feed downgrade: {exc}")


def _root_of(symbol: str) -> str:
    """`ES.v.1` -> `ES.v.0`: which subscription a rank-1 bar belongs to."""
    root, rule, _ = symbol.split(".")
    return f"{root}.{rule}.0"


# DBN files an OHLCV record's size in its `rtype`, which is what lets one session carry
# several schemas at once. Resolved at import from the SDK when it is there, and left as
# the wire constants when it is not, so this module still imports on a box with no SDK —
# which is the whole point of `have_sdk` answering instead of raising.
_TIMEFRAME_OF_RTYPE: dict[int, str] = {33: "1m", 34: "1h", 35: "1d"}
try:                                           # pragma: no cover - exercised by the smoke
    from databento_dbn import RType

    _TIMEFRAME_OF_RTYPE = {int(RType.OHLCV_1M): "1m", int(RType.OHLCV_1H): "1h",
                           int(RType.OHLCV_1D): "1d"}
except Exception:                              # noqa: BLE001 - the literals above stand
    pass


def _smoke() -> None:
    """Prove the gateway end to end without Nautilus: latency, scale, and the archive.

    Streams a few roots for a couple of minutes, then asks the REST archive for the same
    minutes and compares. The two must agree EXACTLY — both are unadjusted rank-0 bars off
    the same vendor — so any difference is a decoding bug in this file, which is what the
    scale check is really for.
    """
    import sys

    symbols = sys.argv[1].split(",") if len(sys.argv) > 1 else ["ES.v.0", "CL.v.0",
                                                                "GC.v.0"]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 180

    class _Log:
        def info(self, m): print(f"  info: {m}", flush=True)
        def warning(self, m): print(f"  WARN: {m}", flush=True)
        def error(self, m): print(f"  ERROR: {m}", flush=True)

    seen: dict[tuple[str, pd.Timestamp], dict] = {}
    delays: list[float] = []
    sent: list[float] = []

    def on_bars(symbol, timeframe, front, behind):
        ts = front.index[-1]
        row = front.iloc[-1]
        # Against the GATEWAY's clock, not this box's — see `gateway_now`. Falling back to
        # the wall clock only when no heartbeat has landed yet, and saying so.
        now = stream.gateway_now()
        local = now is None
        if local:
            now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
        delay = (now - ts - db_live.INTERVALS[timeframe]).total_seconds()
        delays.append(delay)
        seen[(symbol, ts)] = {"Close": float(row["Close"]),
                              "iid": int(row["instrument_id"])}
        exact = stream.latency_seconds()
        if exact is not None:
            sent.append(exact)
        print(f"  {symbol:9s} {ts} close {row['Close']:>12.4f} "
              f"rank1 rows {len(behind):3d}  sent "
              f"{'?' if exact is None else f'{exact:+.2f}'}s after the close"
              f"  (this box saw it {delay:+.0f}s later by "
              f"{'the LOCAL clock, no heartbeat yet' if local else 'the gateway clock, '
                 'which is only heartbeat-granular'})", flush=True)

    stream = FuturesStream(on_bars, lambda mode, why: print(f"  MODE -> {mode}: {why}"),
                           _Log())
    if not stream.start():
        print("the stream would not start; nothing to measure")
        return
    for symbol in symbols:
        stream.subscribe(symbol, "1m")
    print(f"streaming {symbols} at 1m for {seconds}s...", flush=True)
    time.sleep(seconds)
    stream.stop()

    if not delays:
        print("no bars arrived — is the CME open?")
        return
    # Two populations, and mixing them destroys the whole measurement.
    #
    # The catch-up REPLAY is in here too, and it should be — but a bar replayed from
    # twelve minutes ago is legitimately twelve minutes old, and averaging those with the
    # live ones produces a number describing neither. A record the gateway sent within one
    # interval of its own close is live; anything older is the replay, and the spread of
    # THOSE is how far back the replay reached rather than how fast the feed is.
    step = db_live.INTERVALS["1m"].total_seconds()
    live = [x for x in sent if x < step]
    replayed = [x for x in sent if x >= step]
    print(f"\n{len(delays)} bars over {len(symbols)} roots")
    if live:
        print(f"  LIVE      {len(live):3d} sent {min(live):+.2f}s to {max(live):+.2f}s "
              f"after their close, in the GATEWAY's clock (`ts_out`) — against the "
              f"poller's floor of {db_live.ARCHIVE_LAG_SECONDS}s")
    if replayed:
        print(f"  REPLAYED  {len(replayed):3d} up to {max(replayed) / 60:.1f} min old: "
              f"the catch-up that closes the hole a stale REST warm-up leaves")
    print("  this box's wall clock is deliberately NOT used above — measured 42.01s "
          "behind the gateway on 2026-08-28")

    print(f"\nwaiting {db_live.ARCHIVE_LAG_SECONDS}s for the REST archive to catch up, "
          f"then comparing the same minutes...", flush=True)
    time.sleep(db_live.ARCHIVE_LAG_SECONDS)
    for symbol in symbols:
        front, _ = db_live.fetch_raw(symbol, "1m", n=60)
        mine = {ts: v for (s, ts), v in seen.items() if s == symbol}
        both = [ts for ts in mine if ts in front.index]
        if not both:
            print(f"  {symbol:9s} no overlap with the archive yet")
            continue
        bad = [ts for ts in both
               if abs(float(front.loc[ts, "Close"]) - mine[ts]["Close"]) > 1e-9
               or int(front.loc[ts, "instrument_id"]) != mine[ts]["iid"]]
        print(f"  {symbol:9s} {len(both):3d} minutes compared, {len(bad)} disagree"
              + ("" if not bad else f"  <-- {bad[:3]}"))


if __name__ == "__main__":
    _smoke()
