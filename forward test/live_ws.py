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

Run inside `run_paper.py`; there is no separate process to manage.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

import websockets

import paper_state
import td_live

TD_WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
DEFAULT_PORT = 8766
# Ticks arrive far faster than a browser can usefully repaint, and a hundred systems on one
# symbol all revalue from a single print. Marks are coalesced and broadcast on this cadence
# — fast enough to read as live, slow enough that the page is not re-rendering constantly.
BROADCAST_EVERY = 0.5
RECONNECT_MAX = 30


class LiveHub:
    """Owns both sockets and the last price of every instrument."""

    def __init__(self, symbols: list[str], port: int = DEFAULT_PORT) -> None:
        self.symbols = symbols
        self.port = port
        self.prices: dict[str, float] = {}
        self.clients: set = set()
        self._dirty = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.upstream = "connecting"
        self.ticks = 0

    # ------------------------------------------------------------------ upstream
    async def _consume_twelvedata(self) -> None:
        """Subscribe once, then mark on every print. Reconnects forever."""
        backoff = 1
        while not self._stop.is_set():
            try:
                url = f"{TD_WS_URL}?apikey={td_live.api_key()}"
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20) as ws:
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "params": {"symbols": ",".join(self.symbols)},
                    }))
                    self.upstream, backoff = "live", 1
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.upstream = f"reconnecting ({type(exc).__name__})"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

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
                if fails:
                    print(f"twelvedata refused {len(fails)} symbols: "
                          f"{[f.get('symbol') for f in fails][:8]}", flush=True)
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
            ))
        except Exception as exc:
            print(f"tick stream stopped: {exc}", flush=True)

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
