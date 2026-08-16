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

**The JSON is a projection; `store.py` is the record.** This file used to be the only copy
of the desk's history and nothing ever read it back, so each restart began empty and the
forward record restarted at zero. Fills and curve points now go to SQLite as they happen,
and a strategy that has traded before is rehydrated from it on registration. What the
dashboard reads is rendered from that, so the document keeps exactly the shape it had.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paper_config
import store

STATE_PATH = paper_config.RESULTS_DIR / "paper_state.json"

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
    """Drop the in-memory registry and open a new session in the store.

    Clearing is still right: a restart must not resurrect a strategy that is no longer
    configured. What changed is that clearing no longer destroys history — the registry is
    this process's working set, and anything a strategy traded before is read back from the
    database when it registers.
    """
    _strategies.clear()
    _gap_cache.clear()
    store.start_session()


def set_feed(**kw) -> None:
    _feed.update(kw)


def set_venue(**kw) -> None:
    _venue.update(kw)


def register(sid: str, **fields) -> None:
    """Add a strategy to this process's working set, carrying its history forward.

    A system that has traded before comes back with its inception date, its lifetime fill
    count, its trade log and its chained curve. A new one starts empty. Either way the
    document the dashboard sees has the same shape, so nothing downstream can tell the
    difference between "started today" and "resumed".
    """
    _strategies.setdefault(sid, {"id": sid, "trades": [], "paper_curve": [],
                                 "bench_curve": []})
    _strategies[sid].update(fields)

    first_seen = store.upsert_strategy(sid, **fields)
    _record_gap_if_any(sid, fields.get("symbol"), fields.get("tf"))

    lifetime = store.lifetime_curve(sid)
    s = _strategies[sid]
    s["since"] = first_seen[:10]
    s["lifetime_trades"] = store.fill_count(sid)
    s["trades"] = store.recent_fills(sid)
    s["paper_curve"] = lifetime["equity"]
    s["bench_curve"] = lifetime["bench"]
    # Indices where the desk was down. The line is broken there rather than drawn straight
    # through, because a straight segment across four hours of downtime is a claim that
    # nothing happened.
    s["curve_breaks"] = lifetime["breaks"]
    s["gaps"] = lifetime["gaps"]
    s["unknown_gaps"] = lifetime["unknown_gaps"]
    # Age from the STORE's inception, not this process's. A resumed system is already days
    # or weeks old at the moment it registers, and turnover is read off that age.
    try:
        started = datetime.strptime(s["since"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        s["days"] = max((datetime.now(timezone.utc) - started).days, 0)
    except (KeyError, ValueError):
        pass
    _set_turnover(s)


# One benchmark lookup per (symbol, timeframe, gap start), not one per strategy. The desk
# runs 330 systems over 33 symbols, and every system on the same symbol that stopped at the
# same bar is asking the identical question. Without this, startup made 330 sequential
# Twelve Data calls — minutes of boot, and a rate limit waiting to happen.
_gap_cache: dict[tuple, float | None] = {}

# One bar of each timeframe. A gap shorter than this closed no bar, so there is no
# benchmark move to miss and 0.0 is measured rather than assumed.
_BAR_SECONDS = {"1d": 86400.0, "4h": 14400.0}


def _bench_over_gap(symbol: str, timeframe: str, start: datetime) -> float | None:
    key = (symbol, timeframe, start.isoformat())
    if key in _gap_cache:
        return _gap_cache[key]
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    if elapsed < _BAR_SECONDS.get(timeframe, 86400.0):
        _gap_cache[key] = 0.0
        return 0.0
    try:
        import td_live
        value = td_live.return_between(symbol, timeframe, start)
    except Exception:
        value = None              # unknown, and it stays unknown
    _gap_cache[key] = value
    return value


def _record_gap_if_any(sid: str, symbol: str | None, timeframe: str | None) -> None:
    """Measure what the benchmark did while the desk was stopped.

    The strategy's own return across a gap is 0 — it held nothing. The benchmark's is not,
    and assuming it were would flatter the strategy in every falling market. So the actual
    move is fetched for the gap window and stored; if it cannot be fetched the gap is
    recorded with an unknown benchmark instead of a fabricated zero.
    """
    last = store.last_point(sid)
    if last is None or not symbol or not timeframe:
        return
    from_ts = last[0]
    to_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        bench_pct = _bench_over_gap(symbol, timeframe, datetime.fromisoformat(from_ts))
    except Exception:
        bench_pct = None
    store.record_gap(sid, from_ts, to_ts, bench_pct)


def update(sid: str, **fields) -> None:
    """Merge live fields into a registered strategy.

    `since` and `days` are dropped when the store already knows an earlier inception.
    The strategy computes both from its own first bar *this session*, which was right when
    a restart meant starting over and is wrong now — it would relabel a system that has
    traded for weeks as having started this morning, on every restart, forever.
    """
    s = _strategies.get(sid)
    if s is None:
        return
    first_seen = s.get("since")
    if first_seen:
        # Unconditionally, and that is the fix. This used to run only when the caller
        # reported a LATER `since` than the store — but `_export` passes `days` and no
        # `since` at all, so the guard never fired on the one path that runs every bar.
        # The board therefore showed the store's true inception next to a day count
        # measured from the last restart: "since 2026-08-14 · 1 day in", three days later.
        fields.pop("since", None)
        try:
            started = datetime.strptime(first_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            fields["days"] = max((datetime.now(timezone.utc) - started).days, 0)
        except ValueError:
            fields.pop("days", None)
    s.update(fields)
    _set_turnover(s)


def _set_turnover(s: dict) -> None:
    """Round trips per name per year, from the LIFETIME record.

    One definition for every kind of system, computed here rather than in each strategy.
    `book_strategy` and `member_strategy` set it to 0.0 at registration and never touched
    it again, so the desk — which is books end to end — reported "turnover 0.0/yr" under
    1,389 fills. `strategy.py` did compute one, but from `self._n_fills`, a counter that
    resets with the process.

    Divided by the number of names because that is what makes it comparable to the
    backtest's figure, which is per asset. A book of five names doing one round trip each
    has turned over once, not five times.
    """
    fills = s.get("lifetime_trades")
    if not fills:
        s["turnover"] = 0.0
        return
    years = max((s.get("days") or 0) / 365.25, 1.0 / 365.25)
    names = max(s.get("names") or 1, 1)
    s["turnover"] = round(fills / 2.0 / years / names, 2)


def push_point(sid: str, equity_pct: float, bench_pct: float,
               ts: str | None = None) -> None:
    """One point on the strategy's curve, in percent from its own start.

    `ts` is the BAR's timestamp, which is what makes the row idempotent: a warm-up replay
    re-emits the same bars and collapses onto the same rows. Falling back to the wall clock
    would collapse genuinely distinct points that happen to land in the same second.

    Recorded first, then the rendered curve is rebuilt from the store, so what the
    dashboard draws is always the chained lifetime series rather than this session's.
    """
    s = _strategies.get(sid)
    if s is None:
        return
    store.record_point(sid, equity_pct, bench_pct, ts=ts)
    lifetime = store.lifetime_curve(sid)
    s["paper_curve"] = lifetime["equity"]
    s["bench_curve"] = lifetime["bench"]
    s["curve_breaks"] = lifetime["breaks"]
    s["gaps"] = lifetime["gaps"]
    s["unknown_gaps"] = lifetime["unknown_gaps"]


def push_trade(sid: str, ts: str, side: str, qty: float, price: float,
               pnl: float = 0.0, symbol: str = "", ref: str = "") -> None:
    """One fill. `symbol` defaults to the strategy's own when it holds only one.

    A house rule trades a single instrument and never has to think about either argument;
    an order-driven one must pass both, because they are what keep two genuinely distinct
    fills from collapsing into one row. See `store.record_fill`.
    """
    s = _strategies.get(sid)
    if s is None:
        return
    store.record_fill(sid, ts, side, qty, price, pnl,
                      symbol=symbol or s.get("symbol") or "", ref=ref)
    # Re-read rather than append: on a warm-up replay the store drops the duplicate, and
    # appending here would show a fill in the UI that is not in the record.
    s["trades"] = store.recent_fills(sid)
    s["lifetime_trades"] = store.fill_count(sid)
    _set_turnover(s)


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
        cap = s.get("capital")
        if not cap or "cash" not in s:
            continue
        equity = _mark_book(s, prices) if s.get("kind") == "book" else _mark_one(s, prices)
        if equity is None:
            continue
        s["equity"] = round(equity, 2)
        s["paper_pnl_pct"] = round((equity / cap - 1.0) * 100.0, 3)
        n += 1
    if n:
        flush(force=True)
    return n


def marked_symbols() -> list[str]:
    """Every instrument the desk currently needs a price for, from what it is RUNNING.

    The feed used to be pointed at `run_paper.build_plan()`, the automatic per-symbol legs.
    That list is empty whenever the desk runs from the ledger instead — which is the normal
    configuration now (`--top 0`, "no automatic legs") — so both the tick socket and the
    REST fallback were handed an empty symbol list and marked nothing, ever. The desk
    reported `upstream=live` and `0 symbols being marked` for its whole life.

    Derived from the registry rather than from configuration because it has to follow
    registrations that arrive after startup: a book promoted at noon needs its prices from
    noon, and nothing restarts the desk to tell the feed about it.

    A book publishes its names in `holdings`; a single-instrument system carries a real
    ticker in `symbol`. A book's `symbol` is a LABEL ("100 names") and is skipped — asking
    the vendor for it is what a price lookup by `symbol` was doing all along.
    """
    out: set[str] = set()
    for s in _strategies.values():
        for h in (s.get("holdings") or []):
            sym = h.get("symbol")
            if sym:
                out.add(sym)
        sym = s.get("symbol")
        if sym and s.get("kind") != "book":
            out.add(sym)
    return sorted(out)


def _mark_one(s: dict, prices: dict[str, float]) -> float | None:
    """A system holding ONE instrument. `units` is a share quantity."""
    px = prices.get(s.get("symbol"))
    if px is None:
        return None
    s["mark_price"] = px
    return s["cash"] + s.get("units", 0.0) * px


def _mark_book(s: dict, prices: dict[str, float]) -> float | None:
    """A book, revalued name by name from its own holdings.

    **This is what was missing, and it silently disabled marking for the entire desk.**
    The old code looked the strategy's `symbol` up in the price dict — but a book
    registers as `symbol="5 names"`, a LABEL, so the lookup returned None and every book
    was skipped. Every system on this desk is a book, so `mark()` reported a count of
    zero on every tick and P&L only ever moved when a bar closed: exactly the "sits at
    0.00% and reads as broken" failure this function's docstring exists to prevent.

    A book's top-level `units` is `held_count()` — how many NAMES it holds, not a share
    quantity — so the single-instrument arithmetic above is not merely unreachable for a
    book, it would be wrong if it were reached. The per-name quantities live in
    `holdings`, which is also what the board's expanded table draws, so marking them here
    keeps the total and the rows it is made of on the same prices.
    """
    holdings = s.get("holdings")
    if not isinstance(holdings, list):
        return None
    value, seen = 0.0, False
    for h in holdings:
        px = prices.get(h.get("symbol"))
        if px is not None:
            h["mark"] = px
            seen = True
        else:
            px = h.get("mark")
        units = h.get("units") or 0.0
        if px is None:
            if units:                     # a held name with no price: the total would be
                return None               # short by a whole position, so mark nothing
            continue
        h["value"] = round(units * px, 2)
        entry = h.get("entry")
        h["pnl_pct"] = round((px / entry - 1.0) * 100.0, 3) if entry and units else None
        value += units * px
    if not seen:
        return None                       # no fresh price touched this book at all
    return s["cash"] + value


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


# The dashboard is a static page served from its own `web/` directory, so it cannot read
# anything outside that directory. Mirroring the state there is what makes the site live:
# the page polls this file and repaints, instead of showing whatever was baked into
# `data.js` the last time someone ran the build by hand.
#
# The destination comes from `paper_config.PUBLISH_DIR` rather than being computed here,
# because this is the only write the desk makes outside its own folder and that coupling
# belongs somewhere visible. `PUBLISH_DIR` is None when publishing is switched off.
MIRROR_PATH = (paper_config.PUBLISH_DIR / "live.json"
               if paper_config.PUBLISH_DIR is not None else None)


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
