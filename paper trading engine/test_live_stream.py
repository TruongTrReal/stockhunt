"""The tick stream, offline: what it may subscribe to, and how it fails.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_live_stream.py -q

No network and no credentials. `FakeSocket` is a WebSocket that replays a scripted list of
messages and then raises, which is enough to prove the properties that decide whether this
feed can be trusted:

* a reconnect **resubscribes**, rather than reattaching to a subscription nobody holds,
* a stream that goes silent is **acted on and announced**, never left reading `live`,
* arrival is stamped from **this machine's clock** and never from the vendor's
  `timestamp` field,
* and a `cme_futures` symbol is **never** sent to Twelve Data.

That last one is not a hypothetical tidiness check. Twelve Data carries no CME contract
and does not answer "no": `ES` there is Eversource Energy, and the desk would be marking a
futures book against a utility. The other three are all versions of the same failure —
a feed that has stopped while every indicator still reads healthy — which is the one this
folder has already paid fifteen hours for once.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import live_ws
import paper_config


# ------------------------------------------------------------------ the stub

class FakeSocket:
    """A WebSocket that yields scripted frames, then ends the connection.

    Records everything sent, because what the client SAYS on connect — the subscribe, and
    the heartbeats after it — is most of what these tests are about.
    """

    def __init__(self, frames, close_after=True):
        self._frames = list(frames)
        self._close_after = close_after
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def subscribed(self) -> list[str]:
        """The symbol list of the last `subscribe` this socket was sent."""
        subs = [m for m in self.sent if m.get("action") == "subscribe"]
        if not subs:
            return []
        return [s for s in subs[-1]["params"]["symbols"].split(",") if s]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        if self._close_after:
            raise ConnectionError("upstream closed")
        # Hold the connection open with nothing on it — the state the heartbeat and the
        # watchdog both exist for, and the one an iterator that simply ends cannot model.
        await asyncio.Event().wait()
        raise StopAsyncIteration


class FakeConnect:
    """Stands in for `websockets.connect`, handing out one socket per connection."""

    def __init__(self, sockets):
        self._sockets = list(sockets)
        self.opened: list[FakeSocket] = []
        self.urls: list[str] = []

    def __call__(self, url, **_kw):
        self.urls.append(url)
        return self

    async def __aenter__(self):
        ws = self._sockets.pop(0) if self._sockets else FakeSocket([])
        self.opened.append(ws)
        return ws

    async def __aexit__(self, *_exc):
        return False


def _price(symbol, price, timestamp):
    return json.dumps({"event": "price", "symbol": symbol, "price": price,
                       "timestamp": timestamp})


@pytest.fixture
def patched(monkeypatch):
    """No key lookup, no sleeping, no real socket."""
    monkeypatch.setattr(live_ws.td_live, "api_key", lambda: "TEST-KEY")

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *a, **kw):
        return await real_sleep(0, *a, **kw)

    monkeypatch.setattr(live_ws.asyncio, "sleep", fast_sleep)
    return monkeypatch


# ------------------------------------------------------------------ symbol spelling

def test_futures_are_never_streamed():
    """`ES.v.0` may not reach Twelve Data, whichever door it arrives at."""
    futures = paper_config.UNIVERSE["cme_futures"][0]
    assert futures.endswith(".v.0")                     # the spelling this guards
    assert live_ws.streamable(["SPY", futures, "BTC/USD"]) == ["BTC/USD", "SPY"]


def test_the_constructor_filters_too():
    """The hole that was actually open: `run_paper.main` built the hub from `build_plan`,
    which carries the whole futures leg whenever `--top N` does."""
    hub = live_ws.LiveHub(["SPY", "ES.v.0", "GC.v.0", "XAU/USD"])
    assert hub.symbols == ["SPY", "XAU/USD"]


def test_set_symbols_filters_and_reports_change():
    hub = live_ws.LiveHub(["SPY"])
    assert hub.set_symbols(["SPY", "NQ.v.0"]) is False, (
        "adding only a futures symbol changes nothing that can be subscribed to, so it "
        "must not force a reconnect")
    assert hub.set_symbols(["SPY", "QQQ"]) is True
    assert hub.symbols == ["QQQ", "SPY"]


def test_unknown_symbols_are_kept_not_fatal():
    """`CLASS_OF.get`, not `class_of`: a stale name in the published state must not be
    able to stop the desk, which is what `SystemExit` from `class_of` would do."""
    assert live_ws.streamable(["NOT-A-DESK-SYMBOL"]) == ["NOT-A-DESK-SYMBOL"]


# ------------------------------------------------------------------ arrival stamping

def test_arrival_is_stamped_locally_never_from_the_vendor(monkeypatch):
    """The vendor's `timestamp` repeats across a burst — it stamps the BAR, not the tick.

    Measured on this key on 2026-08-28: it sat ~29s from true time while ticks arrived a
    second apart. Read as freshness it would report a live feed as half a minute stale, or
    a stalled feed as current, since a frozen field and a frozen feed are the same number.
    """
    hub = live_ws.LiveHub(["BTC/USD"])
    frozen = 1_000_000_000                       # the same vendor stamp on every tick
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 500.0)
    hub._on_message(_price("BTC/USD", "60000.0", frozen))
    first = hub._last_tick_at
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 507.0)
    hub._on_message(_price("BTC/USD", "60001.0", frozen))

    assert first == 500.0 and hub._last_tick_at == 507.0, (
        "arrival must advance with the local clock even when the vendor's timestamp does "
        "not move at all")
    assert hub.prices["BTC/USD"] == 60001.0


def test_freshness_ignores_a_frozen_vendor_timestamp(monkeypatch):
    hub = live_ws.LiveHub(["BTC/USD"])
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 100.0)
    hub._on_message(_price("BTC/USD", "60000.0", 1_000_000_000))
    assert hub.is_fresh(within=30) is True
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 160.0)
    assert hub.is_fresh(within=30) is False, (
        "60s since the last arrival is stale however recent the vendor's field claims to be")


def test_a_hub_that_never_ticked_is_not_fresh():
    assert live_ws.LiveHub(["BTC/USD"]).is_fresh(within=10_000) is False


def test_a_bad_tick_does_not_advance_freshness(monkeypatch):
    """A malformed or non-positive price is not evidence the feed is alive."""
    hub = live_ws.LiveHub(["BTC/USD"])
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 100.0)
    hub._on_message(_price("BTC/USD", "not-a-number", 1))
    hub._on_message(_price("BTC/USD", "0", 1))
    hub._on_message("{not json")
    assert hub._last_tick_at is None and hub.prices == {}


# ------------------------------------------------------------------ reconnect

def test_reconnect_resubscribes(patched):
    """A dropped connection must send `subscribe` again on the new socket.

    Reattaching without it is the quietest possible failure: the socket is up, the status
    reads `live`, and not one price ever arrives.
    """
    first = FakeSocket([_price("BTC/USD", "60000.0", 1)])
    second = FakeSocket([_price("BTC/USD", "60010.0", 1)])
    connect = FakeConnect([first, second])
    patched.setattr(live_ws.websockets, "connect", connect)

    hub = live_ws.LiveHub(["BTC/USD", "SPY"])

    async def drive():
        task = asyncio.ensure_future(hub._consume_twelvedata())
        for _ in range(400):
            await asyncio.sleep(0)
            if len(connect.opened) >= 2 and second.sent:
                break
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert len(connect.opened) >= 2, "the client must reconnect after the socket closed"
    assert first.subscribed() == ["BTC/USD", "SPY"]
    assert second.subscribed() == ["BTC/USD", "SPY"], (
        "the second connection resubscribed to a different set, or not at all")
    assert all("apikey=TEST-KEY" in u for u in connect.urls)


def test_reconnect_resubscribes_to_the_NEW_set(patched):
    """`set_symbols` re-subscribes by dropping the socket, so the reconnect must carry the
    set as it is NOW — not the one captured when the hub was built."""
    first = FakeSocket([])
    second = FakeSocket([])
    connect = FakeConnect([first, second])
    patched.setattr(live_ws.websockets, "connect", connect)

    hub = live_ws.LiveHub(["BTC/USD"])

    async def drive():
        task = asyncio.ensure_future(hub._consume_twelvedata())
        for _ in range(200):
            await asyncio.sleep(0)
            if first.sent:
                break
        hub.symbols = ["ETH/USD", "GLD"]          # a book was promoted meanwhile
        for _ in range(400):
            await asyncio.sleep(0)
            if second.sent:
                break
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert second.subscribed() == ["ETH/USD", "GLD"]


def test_a_heartbeat_is_sent(patched):
    """The vendor's gateway closes a subscription that never speaks, and `ping_interval`
    is a different channel — answered by the edge, not by the quote service."""
    sock = FakeSocket([], close_after=False)
    connect = FakeConnect([sock])
    patched.setattr(live_ws.websockets, "connect", connect)

    hub = live_ws.LiveHub(["BTC/USD"])

    async def drive():
        task = asyncio.ensure_future(hub._consume_twelvedata())
        for _ in range(2000):
            await asyncio.sleep(0)
            if sum(1 for m in sock.sent if m.get("action") == "heartbeat") >= 2:
                break
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    beats = [m for m in sock.sent if m.get("action") == "heartbeat"]
    assert len(beats) >= 2, f"expected repeated heartbeats, got {sock.sent}"
    assert sock.sent[0]["action"] == "subscribe", "the subscribe must come first"


def test_refused_symbols_are_kept_not_only_printed():
    """A symbol the vendor's plan rejected and a symbol that simply never trades look
    identical on the board unless the refusal is carried."""
    hub = live_ws.LiveHub(["BTC/USD", "NOPE"])
    hub._on_message(json.dumps({"event": "subscribe-status", "status": "ok",
                                "success": [{"symbol": "BTC/USD"}],
                                "fails": [{"symbol": "NOPE"}]}))
    assert hub.refused == ["NOPE"]
    assert "NOPE" in hub.health()["refused"]


# ------------------------------------------------------------------ the silent stall

def test_the_watchdog_forces_a_reconnect_on_a_silent_socket(patched, capsys):
    """A socket that is up and delivering nothing is closed, loudly.

    This is the failure the whole module is shaped around: a dead connection raises and
    recovers on its own, while a live-looking one that has stopped publishing does not —
    and `upstream` describes the connection, so it goes on saying `live`.
    """
    hub = live_ws.LiveHub(["BTC/USD"])
    sock = FakeSocket([], close_after=False)
    hub._ws = sock
    hub._connected_at = 0.0
    patched.setattr(live_ws.time, "monotonic",
                    lambda: live_ws.STALL_AFTER + 1)

    async def drive():
        task = asyncio.ensure_future(hub._watchdog())
        for _ in range(200):
            await asyncio.sleep(0)
            if sock.closed:
                break
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert sock.closed, "a socket silent past STALL_AFTER must be dropped, not tolerated"
    assert hub.health()["state"] == "down"
    said = capsys.readouterr().out
    assert "REST poll" in said, f"the fallback must be announced; log said: {said!r}"


def test_the_watchdog_fires_with_no_tick_ever(patched):
    """Timed from the CONNECT when nothing has ever arrived.

    A watchdog written off `_last_tick_at` alone never fires on a subscription the vendor
    accepted and then never served — the case where every other indicator reads healthy.
    """
    hub = live_ws.LiveHub(["BTC/USD"])
    assert hub._last_tick_at is None
    hub._ws, hub._connected_at = FakeSocket([], close_after=False), 0.0
    patched.setattr(live_ws.time, "monotonic", lambda: live_ws.STALL_AFTER + 1)

    async def drive():
        task = asyncio.ensure_future(hub._watchdog())
        for _ in range(200):
            await asyncio.sleep(0)
            if hub._ws.closed:
                break
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert hub._ws.closed


def test_the_watchdog_leaves_a_merely_quiet_socket_alone(patched):
    """An equity book prints nothing overnight, and that is not a fault."""
    hub = live_ws.LiveHub(["SPY"])
    sock = FakeSocket([], close_after=False)
    hub._ws, hub._connected_at, hub._last_tick_at = sock, 0.0, 0.0
    patched.setattr(live_ws.time, "monotonic", lambda: live_ws.QUIET_AFTER + 1)

    async def drive():
        task = asyncio.ensure_future(hub._watchdog())
        for _ in range(200):
            await asyncio.sleep(0)
        hub._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert not sock.closed
    assert hub.health()["state"] == "quiet", (
        "quiet must not be spelled the same as stalled — one is normal and one is a fault")


# ------------------------------------------------------------------ what gets published

def test_health_states_are_distinguishable(monkeypatch):
    idle = live_ws.LiveHub([])
    assert idle.health()["state"] == "idle"

    hub = live_ws.LiveHub(["BTC/USD"])
    hub.upstream = "live"
    monkeypatch.setattr(live_ws.time, "monotonic", lambda: 100.0)
    hub._on_message(_price("BTC/USD", "60000.0", 1))
    h = hub.health()
    assert h["state"] == "live" and h["ticks"] == 1 and h["symbols"] == 1
    assert h["tick_age_s"] == 0.0 and h["last_tick"].endswith("UTC")

    hub.upstream = "reconnecting (ConnectionError)"
    assert hub.health()["state"] == "down"


def test_the_marker_says_which_source_is_carrying_the_desk(monkeypatch, capsys):
    """The REST poll must never take over in silence.

    A board reading `live` off a socket that stopped an hour ago is worse than one reading
    `polling`, because it is believed.
    """
    import paper_state
    import run_paper

    marks: list[dict] = []
    monkeypatch.setattr(run_paper.td_live, "fetch_prices", lambda syms: {"SPY": 1.0})
    monkeypatch.setattr(paper_state, "mark", lambda prices: marks.append(prices) or 1)
    monkeypatch.setattr(paper_state, "flush", lambda *a, **kw: None)
    published: dict = {}
    monkeypatch.setattr(paper_state, "set_feed", lambda **kw: published.update(kw))

    class Hub:
        fresh = True

        def is_fresh(self, within):
            return self.fresh

    hub = Hub()
    stop = run_paper.start_marker(lambda: ["SPY"], 1, hub)
    time.sleep(1.4)
    assert published.get("marks") == "stream"
    hub.fresh = False                        # the socket stalls
    time.sleep(1.2)
    stop()

    assert published.get("marks") == "poll"
    assert marks, "the poll must keep marking whichever source is nominally carrying"
    said = capsys.readouterr().out
    assert "tick stream" in said and "REST /price poll" in said, (
        f"both transitions must be printed; log said {said!r}")


def test_the_marker_still_runs_with_no_hub(monkeypatch):
    """Streaming is optional (`--ws-port 0`), and the desk must mark without it."""
    import paper_state
    import run_paper

    monkeypatch.setattr(run_paper.td_live, "fetch_prices", lambda syms: {"SPY": 1.0})
    monkeypatch.setattr(paper_state, "mark", lambda prices: 1)
    monkeypatch.setattr(paper_state, "flush", lambda *a, **kw: None)
    published: dict = {}
    monkeypatch.setattr(paper_state, "set_feed", lambda **kw: published.update(kw))

    stop = run_paper.start_marker(lambda: ["SPY"], 1, None)
    time.sleep(1.3)
    stop()
    assert published.get("marks") == "poll"


def test_the_marker_never_asks_twelvedata_for_futures(monkeypatch):
    """`start_marker` batches every marked symbol into one `/price` call, so one futures
    name in the list prices the whole book against Eversource Energy."""
    import paper_state
    import run_paper

    asked: list[list[str]] = []
    monkeypatch.setattr(run_paper.td_live, "fetch_prices",
                        lambda syms: asked.append(list(syms)) or {})
    monkeypatch.setattr(paper_state, "mark", lambda prices: 0)
    monkeypatch.setattr(paper_state, "flush", lambda *a, **kw: None)
    monkeypatch.setattr(paper_state, "set_feed", lambda **kw: None)

    stop = run_paper.start_marker(lambda: ["SPY", "ES.v.0", "BTC/USD"], 1, None)
    time.sleep(1.3)
    stop()
    assert asked and all("ES.v.0" not in batch for batch in asked)
    assert asked[0] == ["SPY", "BTC/USD"]


# ------------------------------------------------------------------ the bar path

# What the vendor was measured doing on 2026-08-28: a bar's close stopped moving 19.7-24.0s
# after its true close, on 8 of 8 one-minute bars and 8 of 8 five-minute ones over four
# symbols. Restated here rather than imported from `td_nautilus`, so that lowering the
# constant there cannot silently lower the bar this test holds it to.
MEASURED_SETTLE_S = 24.0
SHORTENED = ("1m", "5m")


def test_poll_lag_is_shorter_where_measured_and_unchanged_elsewhere():
    """90 seconds is a pause after a daily close and a bar and a half after a 1m one."""
    import td_nautilus as tdn

    assert tdn.poll_lag("1d") == tdn.POLL_LAG == 90
    assert tdn.poll_lag("4h") == 90, "the sizes carrying the live record are not moved"
    for tf in SHORTENED:
        assert tdn.poll_lag(tf) < tdn.POLL_LAG
    for tf in ("15m", "1h", "2h"):
        assert tdn.poll_lag(tf) == tdn.POLL_LAG, (
            f"{tf} was never measured, so it does not get a shortened lag")


@pytest.mark.parametrize("tf", SHORTENED)
def test_the_lag_is_longer_than_the_measured_settle(tf):
    """A lag under the settle time is a LOOK-AHEAD, not merely an early request.

    At close + 15s the interval has fully elapsed, so `fetch_bars`' forming-bar guard keeps
    the row — and the vendor then moves its close for another five seconds. The desk would
    fill against a print that was still changing, and nothing afterwards would show it.
    """
    import td_live as tdl
    import td_nautilus as tdn

    assert tdn.poll_lag(tf) > MEASURED_SETTLE_S, (
        f"poll_lag({tf!r}) = {tdn.poll_lag(tf)}s is inside the vendor's measured "
        f"{MEASURED_SETTLE_S}s aggregation window")
    assert tdn.poll_lag(tf) >= MEASURED_SETTLE_S * 1.5, (
        "keep real headroom: the equity classes were never in the sample")
    assert tdn.poll_lag(tf) < tdl.INTERVALS[tf][1].total_seconds(), (
        "a lag of a whole bar is the problem being fixed")


def test_the_retry_phase_never_outlives_one_bar():
    """`MAX_RETRIES * RETRY_EVERY` is twenty minutes — a slice of a daily bar and twenty
    BARS at 1m, which overlaps the next twenty polls chasing a superseded bar."""
    import td_live as tdl
    import td_nautilus as tdn

    for tf in paper_config.MEMBER_TIMEFRAMES:
        span = tdl.INTERVALS[tf][1].total_seconds()
        window = tdn.max_retries(tf) * tdn.retry_every(tf)
        assert window <= max(span, 60), f"{tf}: retries for {window}s over a {span}s bar"
        assert tdn.max_retries(tf) >= 3, f"{tf}: a single slow settle must not lose the bar"


def test_a_shortened_lag_is_never_slower_than_the_old_one():
    """The floor property: the first attempt is earlier than it used to be AND a miss is
    retried sooner, so no timeframe can come out of this change worse than it went in."""
    import td_nautilus as tdn

    for tf in paper_config.MEMBER_TIMEFRAMES:
        assert tdn.poll_lag(tf) <= tdn.POLL_LAG
        assert tdn.retry_every(tf) <= tdn.RETRY_EVERY
        assert (tdn.poll_lag(tf) + tdn.retry_every(tf)
                <= tdn.POLL_LAG + tdn.RETRY_EVERY)
