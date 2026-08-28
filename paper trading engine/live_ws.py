"""Live tick streaming: Twelve Data -> this process -> the browser.

Two WebSockets, one hop each:

    Twelve Data  --wss-->  this node  --ws-->  the dashboard

The polling version this replaces asked `/price` for every symbol once a minute. That is
fine for a system that decides on daily bars, but it makes the page feel dead: a position
sits at one number for sixty seconds and then jumps. Streaming marks each position the
instant its instrument prints, so P&L moves the way a screen is expected to move.

**Marks are display only, and that separation is the whole safety argument.** A tick never
touches a target, never places an order and never reaches a rule. Positions are still
decided on closed bars by `TalibRuleStrategy`, which is what keeps the live desk comparable
to the backtest — a strategy that traded on an unfinished bar would be testing something
the research never measured. Everything here revalues an *existing* position; it cannot
open, close or resize one.

**Both sockets are failure-tolerant by design.** The upstream reconnects with backoff and
the browser server drops slow clients, because neither is allowed to interfere with
trading. If this whole module dies the desk keeps running and the dashboard falls back to
polling `live.json`.

**There is ONE upstream socket and there may only ever be one.** Twelve Data meters
concurrent WebSocket connections per API key, not per process, so a second client opened
anywhere in this repo does not add a feed — it evicts this one, and the eviction looks
exactly like a flaky network. Anything that wants live prices reads `LiveHub.prices` or
`LiveHub.health()`; it does not open its own connection. That is why the stream lives here
rather than in `td_live.py` beside the REST calls: `td_live` is imported by
`parity_live.py`, by the dashboard's build and by the tests, and a socket in it would be
opened by things that only wanted a DataFrame.

**A stream that stops quietly is the failure this file is built around.** A dead socket
raises and reconnects; a socket that stays open and delivers nothing does not, and the
status field describes the connection rather than the data. `td_nautilus.timeframe_of`'s
docstring is the fifteen-hour version of that same mistake one layer down. So there are
three defences here and none of them is optional:

* an application-level **heartbeat**, because the vendor's gateway closes a connection
  that never speaks and the protocol ping is a different channel (see `HEARTBEAT_EVERY`),
* a **watchdog** that forces a reconnect when nothing has arrived for `STALL_AFTER`, timed
  from the CONNECT when not one tick has ever landed — the case a "last tick" timer cannot
  see at all,
* and a **published state** that says which of the two sources is actually carrying the
  desk, so falling back to the REST poll is announced rather than silent.

Run inside `run_paper.py`; there is no separate process to manage.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone

import websockets

import paper_config
import paper_state
import td_live

TD_WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
DEFAULT_PORT = 8766
# Ticks arrive far faster than a browser can usefully repaint, and a hundred systems on one
# symbol all revalue from a single print. Marks are coalesced and broadcast on this cadence
# — fast enough to read as live, slow enough that the page is not re-rendering constantly.
BROADCAST_EVERY = 0.5
RECONNECT_MAX = 30

# An APPLICATION-level heartbeat, which is not the same thing as `websockets`'
# `ping_interval` and does not replace it. The protocol ping is answered by the vendor's
# gateway; the heartbeat is answered by the quote service behind it, and it is the one the
# vendor documents as required to keep a subscription alive. A connection kept open by
# pings alone still reads as `live` here while the service behind it has stopped sending —
# which is precisely the silent stall this module exists to make impossible.
HEARTBEAT_EVERY = 10

# Two silences, and they are treated differently on purpose.
#
# `QUIET_AFTER` is REPORTED and nothing else: an equity book at 03:00 UTC legitimately
# prints nothing for hours, and reconnecting over that would be noise. `STALL_AFTER` is
# ACTED ON — the socket is closed so the existing reconnect path resubscribes.
#
# Five minutes is safe to act on even on a shut market, because the cost of being wrong is
# one TCP connect, and the cost of being right is the difference between a desk marking
# live prices and a desk marking whatever it last saw. The desk cannot tell "market shut"
# from "socket wedged" by looking at the socket, and a reconnect is the cheap way to ask.
QUIET_AFTER = 90
STALL_AFTER = 300


def streamable(symbols) -> list[str]:
    """The desk symbols Twelve Data may be asked to stream, sorted and de-duplicated.

    **`cme_futures` is removed here, at the capability, and not only at the call sites.**
    Asking Twelve Data for `ES.v.0` is the "a bare ticker is not an identity" failure the
    root `CLAUDE.md` documents: the vendor carries no CME contract, so the name comes back
    in `subscribe-status.fails` forever — one more line of noise per reconnect about a
    symbol that was never going to print — and an unqualified `ES` there is Eversource
    Energy, a clean and entirely wrong series. `run_paper.start_feed_tracker` already split
    the running book by vendor before calling `set_symbols`, but the hub's CONSTRUCTOR was
    handed `build_plan`'s symbols unfiltered, so `--top 3` put the whole futures leg into
    the very first subscription. A guard at one of two doors is a guard on neither.

    `CLASS_OF.get`, not `class_of`, because the latter raises `SystemExit` on an unknown
    symbol and a price feed must not be able to stop the desk over a stale name in the
    published state — the same reason `run_paper._split_by_feed` uses the dict.
    """
    return sorted({s for s in symbols
                   if s and paper_config.CLASS_OF.get(s) != "cme_futures"})


class LiveHub:
    """Owns both sockets and the last price of every instrument."""

    def __init__(self, symbols: list[str], port: int = DEFAULT_PORT) -> None:
        self.symbols = streamable(symbols)
        self.port = port
        self.prices: dict[str, float] = {}
        self.clients: set = set()
        self._dirty = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # The live upstream socket, so `set_symbols` can drop it and force a re-subscribe.
        self._ws = None
        self.upstream = "connecting"
        self.ticks = 0
        # Which symbols the vendor's plan refused, kept rather than only printed: a symbol
        # that never streams and a symbol that was rejected look identical on the board.
        self.refused: list[str] = []
        # **Arrival, taken from THIS machine's clock.** The vendor's `timestamp` field is
        # not the tick time — it repeats across a burst of prints, because it stamps the
        # BAR the tick belongs to, and it was measured ~37s from local time on this key.
        # Anything that reads it as a freshness signal concludes the feed is a minute
        # behind when it is current, or current when it has stopped. `td_live`'s module
        # docstring has said so since before there was a streaming path; this is the path.
        self._last_tick_at: float | None = None
        self._connected_at: float | None = None
        self.last_tick_utc: str | None = None

    # ------------------------------------------------------------------ upstream
    def set_symbols(self, symbols) -> bool:
        """Point the subscription at a new set, and re-subscribe if it changed.

        The desk does not know what it will trade at the moment this hub is built: with no
        automatic legs it runs whatever is in the ledger, and `desk_control` attaches those
        books after the node is already up. A subscription fixed at construction is
        therefore a subscription to nothing at all — which is exactly what shipped.

        Re-subscribing by closing the socket rather than by sending an `unsubscribe`: the
        reconnect path is already written, already tested by every dropped connection, and
        cannot leave the two sides disagreeing about what is subscribed.
        """
        new = streamable(symbols)
        if new == self.symbols:
            return False
        self.symbols = new
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        return True

    async def _consume_twelvedata(self) -> None:
        """Subscribe, then mark on every print. Reconnects forever, and re-subscribes
        whenever `set_symbols` changes the set."""
        backoff = 1
        while not self._stop.is_set():
            # Nothing registered yet. Connecting here would subscribe to the empty string
            # and sit there reporting `live` while receiving nothing, which is how this
            # went unnoticed: the status field described the socket, not the subscription.
            if not self.symbols:
                self.upstream = "idle (nothing to price yet)"
                await asyncio.sleep(2)
                continue
            heartbeat = None
            try:
                url = f"{TD_WS_URL}?apikey={td_live.api_key()}"
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20) as ws:
                    self._ws = ws
                    # Reset on every connect, and BEFORE the subscribe, so the watchdog
                    # measures this connection rather than the last one. A socket that
                    # connects and never delivers a single tick has no "last tick" to time
                    # from — that is the case a naive freshness timer cannot see, and it
                    # is the shape of the failure that gets believed, because the status
                    # field says `live` and means "the TCP connection is up".
                    self._connected_at = time.monotonic()
                    self._last_tick_at = None
                    self.refused = []
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "params": {"symbols": ",".join(self.symbols)},
                    }))
                    self.upstream, backoff = "live", 1
                    heartbeat = asyncio.ensure_future(self._heartbeat(ws))
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._on_message(raw)
                # A CLEAN close — the vendor hung up politely, or `set_symbols` and the
                # watchdog closed the socket on purpose — ends the iterator without
                # raising, so it never reaches the backoff below. Reconnecting instantly
                # is right for a deliberate re-subscribe and is a hot loop if the far end
                # keeps hanging up: one connect per iteration, as fast as the network
                # allows, on the same event loop the browser socket is served from. One
                # second is invisible to a re-subscribe and turns that loop into a
                # once-a-second retry.
                if not self._stop.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.upstream = f"reconnecting ({type(exc).__name__})"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            finally:
                self._ws = None
                self._connected_at = None
                if heartbeat is not None:
                    heartbeat.cancel()

    async def _heartbeat(self, ws) -> None:
        """Tell the vendor we are still here, on the channel the vendor listens on.

        Twelve Data's gateway closes a subscription that never sends anything, and the
        `ping_interval` above does **not** satisfy it: a protocol ping is answered by the
        edge, while the heartbeat is answered by the quote service behind it. The two
        failure modes that distinction produces are the ones worth naming — a connection
        dropped mid-session for saying nothing, which at least raises and reconnects, and a
        connection kept open by pings while the service behind it has stopped publishing,
        which does not raise and reads as healthy forever.

        Failing here closes the socket rather than being swallowed, because a heartbeat
        that silently stopped is a subscription on borrowed time and the reconnect path is
        the thing that fixes it.
        """
        while not self._stop.is_set():
            await asyncio.sleep(HEARTBEAT_EVERY)
            await ws.send(json.dumps({"action": "heartbeat"}))

    async def _watchdog(self) -> None:
        """Force a reconnect when a live-looking socket has stopped delivering.

        Timed from the last tick, or from the CONNECT when there has never been one. The
        second half is the important one: a subscription the vendor accepted and then never
        served has no last tick, so a watchdog written the obvious way never fires on it —
        and that is the case where every other indicator reads healthy.

        Closing the socket rather than resubscribing over it, for the reason `set_symbols`
        gives: the reconnect path is already written and already exercised by every dropped
        connection, and it cannot leave the two sides disagreeing about what is subscribed.
        """
        while not self._stop.is_set():
            await asyncio.sleep(1)
            ws = self._ws
            since = self._last_tick_at or self._connected_at
            if ws is None or since is None or not self.symbols:
                continue
            if time.monotonic() - since < STALL_AFTER:
                continue
            print(f"tick stream: nothing in {STALL_AFTER}s over {len(self.symbols)} "
                  f"symbols — forcing a reconnect. Marks are on the REST poll until it "
                  f"comes back.", flush=True)
            self.upstream = "stalled — reconnecting"
            self._last_tick_at = None
            self._connected_at = time.monotonic()   # do not fire again while it reconnects
            try:
                await ws.close()
            except Exception:
                pass

    def _on_message(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if msg.get("event") != "price":
            # `subscribe-status` reports which symbols the plan actually accepted; a
            # silently-rejected symbol would otherwise look like an instrument that simply
            # never trades.
            if msg.get("event") == "subscribe-status":
                fails = msg.get("fails") or []
                self.refused = [f.get("symbol") for f in fails if f.get("symbol")]
                if fails:
                    print(f"twelvedata refused {len(fails)} symbols: "
                          f"{self.refused[:8]}", flush=True)
            return
        symbol, price = msg.get("symbol"), msg.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        self.prices[symbol] = price
        self.ticks += 1
        self._dirty = True
        # **Stamped here, from this machine's clock, and never from `msg["timestamp"]`.**
        # The vendor's field is the stamp of the BAR the tick belongs to: it repeats
        # unchanged across a whole burst of prints and was measured ~37s from local time on
        # this key. Used as a freshness signal it would report a current feed as a minute
        # stale — or, far worse, a stalled feed as current, since a frozen field and a
        # frozen feed are the same number. Everything downstream that asks "how old is
        # this price" reads the value set on the next two lines.
        self._last_tick_at = time.monotonic()
        self.last_tick_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ------------------------------------------------------------------ is it alive?
    def is_fresh(self, within: float) -> bool:
        """Has a tick landed in the last `within` seconds?

        This is the question the REST marker asks to decide which source is carrying the
        desk, and it is deliberately about DATA rather than about the socket. `upstream`
        can say `live` for a connection that has delivered nothing since it opened.
        """
        if self._last_tick_at is None:
            return False
        return (time.monotonic() - self._last_tick_at) < within

    def health(self) -> dict:
        """What to publish about the stream, in the words the board should print.

        `state` distinguishes the three things a reader needs to tell apart and that one
        boolean cannot: streaming, connected-but-silent, and broken. `quiet` is not an
        error — a US equity book prints nothing overnight — which is exactly why it must
        not be spelled the same as `stalled`.
        """
        age = None
        if self._last_tick_at is not None:
            age = round(time.monotonic() - self._last_tick_at, 1)
        if not self.symbols:
            state = "idle"
        elif self.upstream.startswith(("reconnecting", "stalled", "stopped")):
            state = "down"
        elif age is None or age > QUIET_AFTER:
            state = "quiet"
        else:
            state = "live"
        return {"state": state, "upstream": self.upstream, "ticks": self.ticks,
                "symbols": len(self.symbols), "tick_age_s": age,
                "last_tick": self.last_tick_utc,
                "refused": self.refused[:8]}

    # ------------------------------------------------------------------ downstream
    async def _serve_browsers(self) -> None:
        async def handler(ws):
            self.clients.add(ws)
            try:
                await ws.send(json.dumps(self._payload(full=True)))
                async for _ in ws:          # the page never sends; this just holds it open
                    pass
            except Exception:
                pass
            finally:
                self.clients.discard(ws)

        async with websockets.serve(handler, "127.0.0.1", self.port,
                                    ping_interval=20, ping_timeout=20):
            print(f"tick stream on ws://127.0.0.1:{self.port} "
                  f"({len(self.symbols)} symbols)", flush=True)
            await asyncio.Future()          # serve until the loop is stopped

    async def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(BROADCAST_EVERY)
            # The stream's state goes into the published document on EVERY pass, dirty or
            # not, and that is the whole point of putting it here: the pass that matters is
            # the one where nothing arrived. Only the in-memory `_feed` is touched, so this
            # costs no write — whoever flushes next (a mark, or the marker's own heartbeat
            # write) carries the current state out with it.
            try:
                paper_state.set_feed(stream=self.health())
            except Exception:
                pass
            if not self._dirty:
                continue
            self._dirty = False
            # Mark first, then send: the payload is built from the marked registry, so the
            # numbers on screen are the same ones written to `paper_state`.
            try:
                paper_state.mark(self.prices)
            except Exception:
                continue
            if not self.clients:
                continue
            msg = json.dumps(self._payload())
            for ws in list(self.clients):
                try:
                    await ws.send(msg)
                except Exception:
                    self.clients.discard(ws)

    def _payload(self, full: bool = False) -> dict:
        """Only what moves. The full document is 300 kB and is already available over HTTP;
        pushing it twice a second would be wasteful and would make the page re-parse
        everything to update a few numbers."""
        rows = [{"id": s["id"], "pnl": s.get("paper_pnl_pct"),
                 "eq": s.get("equity"), "px": s.get("mark_price"),
                 "u": s.get("units"), "st": s.get("state"),
                 "n": s.get("paper_trades")}
                for s in paper_state.snapshot()["strategies"]]
        return {"t": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "upstream": self.upstream, "ticks": self.ticks,
                "prices": self.prices if full else
                          {k: v for k, v in self.prices.items()},
                "rows": rows, "full": full}

    # ------------------------------------------------------------------ lifecycle
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(asyncio.gather(
                self._consume_twelvedata(),
                self._serve_browsers(),
                self._broadcast_loop(),
                self._watchdog(),
            ))
        except Exception as exc:
            # Loud in the log AND in the published state. A thread that dies leaves
            # `upstream` frozen at whatever it last said, so the board would go on
            # reporting `live` for a stream that no longer exists — the same "a status
            # field cannot report a death" problem `run_paper.start_marker` solves for the
            # process as a whole. The REST marker keeps the marks moving from here.
            #
            # **A deliberate stop is not that**, and must not be dressed as it. `stop()`
            # stops the loop out from under these coroutines, so a clean shutdown always
            # raises here — announcing a fallback on every ordinary exit is how a warning
            # that matters gets learned as noise.
            deliberate = self._stop.is_set()
            self.upstream = "stopped" if deliberate else f"stopped ({type(exc).__name__})"
            try:
                paper_state.set_feed(stream=self.health())
            except Exception:
                pass
            print(f"tick stream stopped: {exc}"
                  + ("" if deliberate else " — marks fall back to the REST poll"),
                  flush=True)

    def start(self) -> "LiveHub":
        # A daemon thread with its own loop, kept off the Nautilus loop entirely. Nothing
        # here may ever delay a bar or an order.
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-ws")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
