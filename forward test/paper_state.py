"""The bridge between a running node and the dashboard: `results/paper_state.json`.

`web/build_web_data.py` reads this file and renders whatever it finds. Nothing wrote it
before, which is why the paper section of the site showed zero strategies while the node
was perfectly capable of running — the numbers existed only inside a live process and
died with it.

Design notes that matter:

**A module-level registry, not a per-strategy file.** The node runs several strategies in
one process, and the dashboard wants one document describing all of them. Each strategy
registers on start and pushes a snapshot whenever something changes; the writer
serialises the whole registry each time. At 1d/4h bar rates that is a handful of writes a
day — the cost is irrelevant and the alternative (merging N files, some stale) is not.

**Written atomically.** The dashboard may fetch the file at any moment, including
mid-write. Writing to a temp file in the same directory and `os.replace`-ing it makes the
swap atomic on Windows and POSIX alike, so a reader sees either the old document or the
new one and never a truncated one.

**Absent means absent.** If the node has never run, there is no file, and the dashboard
says "not running". That is the honest state and it must never be pre-seeded with an
empty scaffold that looks like a live desk reporting zeroes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import fwd_config

STATE_PATH = fwd_config.RESULTS_DIR / "paper_state.json"

# Curves are sampled per bar and the dashboard draws them ~90px wide; more than this is
# invisible detail that grows the file the browser has to parse on every load.
MAX_CURVE_POINTS = 400
MAX_TRADES = 200

# Shortest gap between whole-document writes. Two seconds collapses a bar-boundary burst
# to one or two writes while keeping the dashboard effectively live.
MIN_FLUSH_SECONDS = 2.0

_write_lock = threading.Lock()
_last_write = 0.0
_timer: threading.Timer | None = None

_strategies: dict[str, dict] = {}
_venue: dict = {"name": "Nautilus sandbox", "balance": 0.0, "equity": 0.0}
_feed: dict = {"source": "Twelve Data", "plan": "pro", "status": "starting", "last_bar": "—"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def reset() -> None:
    """Drop everything. Called at node start so a restart does not resurrect a strategy
    that is no longer configured."""
    _strategies.clear()


def set_feed(**kw) -> None:
    _feed.update(kw)


def set_venue(**kw) -> None:
    _venue.update(kw)


def register(sid: str, **fields) -> None:
    _strategies.setdefault(sid, {"id": sid, "trades": [], "paper_curve": [],
                                 "bench_curve": []})
    _strategies[sid].update(fields)


def update(sid: str, **fields) -> None:
    if sid in _strategies:
        _strategies[sid].update(fields)


def push_point(sid: str, equity_pct: float, bench_pct: float) -> None:
    """One point on the strategy's curve, in percent from its own start."""
    s = _strategies.get(sid)
    if s is None:
        return
    s["paper_curve"].append(round(float(equity_pct), 4))
    s["bench_curve"].append(round(float(bench_pct), 4))
    # Halve by stride rather than dropping the head: the curve must keep its origin, or
    # the chart silently re-bases and the line stops meaning "since inception".
    if len(s["paper_curve"]) > MAX_CURVE_POINTS:
        s["paper_curve"] = s["paper_curve"][::2]
        s["bench_curve"] = s["bench_curve"][::2]


def push_trade(sid: str, ts: str, side: str, qty: float, price: float,
               pnl: float = 0.0) -> None:
    s = _strategies.get(sid)
    if s is None:
        return
    s["trades"].append({"ts": ts, "side": side, "qty": qty,
                        "price": price, "pnl": round(float(pnl), 2)})
    if len(s["trades"]) > MAX_TRADES:
        s["trades"] = s["trades"][-MAX_TRADES:]


def mark(prices: dict[str, float]) -> int:
    """Revalue every open position at the current price.

    Without this the dashboard's P&L only moves when a bar closes, because that is the only
    moment a strategy recomputes anything. On a daily system that means one update every 24
    hours, and the number sits at exactly 0.00% from the moment the position opens — which
    reads as broken rather than as "nothing has been marked yet".

    Marking is display only. It never touches a target, never places an order and never
    feeds a rule: positions are still decided on closed bars. It answers "what is the open
    position worth right now", which is a different question from "what should I hold".
    """
    n = 0
    for s in _strategies.values():
        px = prices.get(s.get("symbol"))
        cap = s.get("capital")
        if px is None or not cap or "cash" not in s:
            continue
        equity = s["cash"] + s.get("units", 0.0) * px
        s["equity"] = round(equity, 2)
        s["paper_pnl_pct"] = round((equity / cap - 1.0) * 100.0, 3)
        s["mark_price"] = px
        n += 1
    if n:
        flush(force=True)
    return n


def snapshot() -> dict:
    strategies = sorted(_strategies.values(), key=lambda s: (s.get("cls", ""),
                                                            s.get("tf", ""),
                                                            s.get("symbol", "")))
    venue = dict(_venue)
    aggregate = venue.pop("aggregate", True)
    if aggregate:
        # Sum every system's OWN equity. This used to take one figure per venue, which was
        # right when strategies sized against the shared account and all reported the same
        # balance — summing then would have multiplied the desk by the number of rules on
        # it. Each system now keeps its own cash and units, so the reverse is true: taking
        # one per venue reported a $3.3M desk as $19,992, because it counted two books out
        # of three hundred and thirty.
        # A system that has not been marked yet is worth exactly the capital it was given.
        # Skipping it instead made the desk look like it had lost that money: one strategy
        # still warming up subtracted its whole $10,000 book from equity while `balance`
        # still counted it, which flipped a +0.11% desk into −0.19%. Equity and balance have
        # to be summed over the same set or the difference between them is not a P&L.
        books = [s["equity"] if s.get("equity") is not None else (s.get("capital") or 0.0)
                 for s in strategies]
        if books:
            venue["equity"] = round(sum(books), 2)
    return {"generated_at": _now(), "feed": dict(_feed), "venue": venue,
            "strategies": strategies}


# The dashboard is a static page served from `web/`, so it cannot read anything outside
# that directory. Mirroring the state there is what makes the site live: the page polls
# this file and repaints, instead of showing whatever was baked into `data.js` the last
# time someone ran the build by hand.
MIRROR_PATH = fwd_config.HERE / "web" / "live.json"


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot(), indent=1)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    if MIRROR_PATH is not None:
        try:
            MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
            mtmp = MIRROR_PATH.with_suffix(".json.tmp")
            mtmp.write_text(payload, encoding="utf-8")
            os.replace(mtmp, MIRROR_PATH)
        except OSError:
            pass          # the dashboard going stale must never stop the desk trading
    return path


def flush(path: Path | None = None, force: bool = False) -> Path:
    """Serialise the registry. Atomic, so a concurrent reader never sees a partial file.

    Debounced, because every strategy calls this on every bar it closes. With one symbol
    that is a handful of writes a day; with 33 symbols across two timeframes a single 4h
    boundary lands ~165 exports within a few seconds, and writing the whole document 165
    times is pure waste — only the last one is ever read.

    The trailing timer is the part that matters: a plain rate limiter would drop the final
    update of a burst and leave the dashboard stale until the next bar, which at 4h is four
    hours of wrong numbers. Here a skipped write schedules itself instead.
    """
    global _last_write, _timer
    path = path or STATE_PATH
    with _write_lock:
        now = time.monotonic()
        if force or now - _last_write >= MIN_FLUSH_SECONDS:
            _last_write = now
            if _timer is not None:
                _timer.cancel()
                _timer = None
            return _write(path)
        if _timer is None:
            _timer = threading.Timer(MIN_FLUSH_SECONDS, lambda: flush(path, force=True))
            _timer.daemon = True
            _timer.start()
    return path
