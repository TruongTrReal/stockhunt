"""Twelve Data as a live bar source, and the Nautilus data client that wraps it.

**Why BARS are REST and only marks are streamed.** This file used to say the WebSocket
"is the right tool only if the study ever goes intraday", and the study has gone intraday
— `paper_config.MEMBER_TIMEFRAMES` now offers 1m. That did not move the bars, and the
reason is worth stating because it is the whole discipline of this desk:

*The stream delivers TICKS, and the research is built on the vendor's BARS.* The backtest
cache comes from `/time_series`, and `SandboxExecutionClient` prices every fill at the
signal bar's close, so the live record is comparable to the sheet only for as long as both
sides mean the same thing by "the bar". Bars aggregated here from ticks would be *this
desk's* bars — near enough to the vendor's to pass every eye test, different enough to
make the forward test measure something the research never scored, and nothing would say
so. So bars stay on `/time_series`, unchanged, and the socket is used for the two things
that need no bar definition at all: marking open positions continuously, and knowing
whether the feed is alive. That lives in `live_ws.py`, which owns the single upstream
connection.

What the stream prompted on the bar path was a measurement, not an aggregation. A
1-minute bar's close was timed stopping ~20 seconds after that bar's true close, so
`POLL_LAG` no longer has to be 90 seconds at `1m` — see `td_nautilus.POLL_LAG_BY_TF`,
which also records the two ways of measuring it wrong.

**Two vendor behaviours this has to defend against.**

*The forming bar.* `/time_series` returns the current, still-open bar as its most recent
row. Acting on it is look-ahead of the worst kind — the close changes after you trade. So
every read discards the newest row unless that bar's interval has fully elapsed. This is
the single most important line in the file, and **the whole of it is which clock the
question is asked in**: the vendor stamps a naive wall clock, this repo's cache keeps each
class on its own, and a test that reads a New York stamp against a UTC `now` does not fire.
`_without_forming` and `_now_in_cache_clock` are that, in two directions — a commodity bar
was discarded on every read, an intraday equity bar was kept while still forming.

*The grid.* A restamp moves a bar's LABEL. It only moves its WINDOWS when the bar is wider
than the offset, which is commodity `4h` and nothing else today — see `derived_from`, which
builds that cell from `1h` instead of fetching it, so the desk trades the same buckets the
research sheet holds.

*The frozen tick timestamp.* On the WebSocket, `timestamp` repeats across a burst of ticks
— it is the bar stamp, not the tick time. Nothing here depends on it, but any future
streaming path must stamp arrival locally instead.

The Nautilus side is a `LiveMarketDataClient` that answers `_request_bars` from the vendor
(the strategy's warmup) and services `_subscribe_bars` by polling on the bar cadence. That
keeps `backtest -> paper -> live` one code path: only the execution client changes.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import paper_config

BASE_URL = "https://api.twelvedata.com"
OUTPUT_SIZE = 5000

# Vendor interval strings, and how long one bar lasts. The 4h duration is nominal: a US
# equity session is one 4h bar plus a ~2.5h stub, so the stub bar closes early. That only
# ever makes the freshness test stricter, which is the safe direction — and the same is
# true of every intraday timeframe below it, for the same reason.
#
# Taken from the engine's own `TIMEFRAMES` (re-exported by `paper_config`), which already
# names the vendor interval for each one.
# Restating them here is how this map and the engine's came to disagree about which
# timeframes exist at all.
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
INTERVALS = {
    tf: (spec["interval"], timedelta(seconds=int(tf[:-1]) * _UNIT_SECONDS[tf[-1]]))
    for tf, spec in paper_config.TIMEFRAMES.items()
}


def api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    env = paper_config.REPO / ".env.local"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TWELVEDATA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("No Twelve Data API key (env TWELVEDATA_API_KEY or .env.local)")


def _to_frame(values: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    cols = {"open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"}
    for src, dst in cols.items():
        df[dst] = pd.to_numeric(df[src], errors="coerce") if src in df else float("nan")
    return df[list(cols.values())]


def class_of(symbol: str) -> str | None:
    """Which asset class a live symbol belongs to, or None.

    Read only to decide a TIMEZONE — `config.INTRADAY_CLOCK` is keyed on the class, and
    reading a Sydney-stamped commodity bar as UTC put the whole leg permanently one bar
    behind for as long as the leg existed.

    **The desk's own legs are consulted first, and the research universe second.** The two
    disagree in both directions and each disagreement is a real symbol:

    * `XLK` is on this desk's `us_etfs` leg and is NOT in `bt_config.US_ETFS` — the
      liquidity screen dropped it at 19.8 tradable years — so asking the research alone
      answers None for an instrument the desk holds a position in.
    * A symbol admitted at runtime by `paper_config.admit` is in no research universe by
      definition. It has a class the moment it is admitted, because the registration
      declared one and `symbol_resolve` checked it against the vendor, and that is the
      class its bars must be stamped on.

    None for a symbol neither knows, which is the previous behaviour and the right one:
    the callers below then leave the vendor's stamps alone.
    """
    cls = paper_config.CLASS_OF.get(symbol)
    if cls is not None:
        return cls
    try:
        return paper_config.research_class_of(symbol)
    except (KeyError, SystemExit):
        return None


def _to_cache_clock(index: pd.DatetimeIndex, symbol: str) -> pd.DatetimeIndex:
    """Vendor stamps -> the clock this symbol's BACKTEST CACHE is on. Still naive.

    **The vendor stamps a naive wall clock and does not always say which one.** For
    commodities it says nothing at all (`meta.exchange_timezone` is `null`) and stamps
    `Australia/Sydney`; see `config.INTRADAY_CLOCK` for how that was measured.

    Reading a Sydney stamp as UTC put every commodity bar 10-11 hours in the FUTURE, and
    two things downstream believed it. `fetch_bars`' forming-bar guard asks whether a full
    interval has elapsed since the newest bar opened — against a future stamp it never has,
    so **the newest bar was discarded on every single read and the commodity legs of this
    desk ran permanently one bar behind**, silently, with no error anywhere. And `_to_bar`
    in `td_nautilus` stamped `ts_event` from the same value, so the trading record in
    `results/paper.db` carries those bars in the future too.

    **The target is the cache's clock and not simply UTC**, which is the same rule the
    whole desk runs on: a live bar has to mean what the sheet it was selected from means.
    Today that makes this a no-op for four of the five classes and a 10/11-hour shift for
    commodities — `config.INTRADAY_CLOCK` is where that is decided, not here.

    Identity for a class with no declaration and for a symbol no class claims, which is
    exactly the previous behaviour.

    **It leaves the two equity classes on `America/New_York`, and that is correct** — their
    cache is exchange-local, so matching it is the whole point of this function. What used
    to be wrong was the COMPARISON downstream, not this conversion: see
    `_now_in_cache_clock`.
    """
    cls = class_of(symbol)
    if cls is None:
        return index
    return paper_config.to_cache_clock(index, cls)


def _now_in_cache_clock(symbol: str) -> pd.Timestamp:
    """`now`, on the same wall clock this symbol's intraday bars are stamped in. Naive.

    **The forming-bar guard has to compare two readings of one clock, and for the equity
    classes it was comparing two different clocks.** `_to_cache_clock` leaves `us_stocks`
    and `us_etfs` on `America/New_York`, because that is what their cache is stamped in;
    the guard then tested that ET stamp against `datetime.now(timezone.utc)`, which is 4-5
    hours AHEAD of it. `now < last_open + duration` was therefore true only in the small
    hours, so on every intraday equity read the guard did not fire and **the desk kept the
    still-forming bar** — a rule computed from a high, a low and a close that had not
    finished happening. That is the same defect the commodity Sydney bug was, in the
    opposite direction: there a future stamp discarded a good bar, here a past stamp keeps
    a bad one, and keeping a bad one is the expensive half.

    **The fix converts `now`, not the bar.** Moving the bar onto UTC would move `ts_event`
    by 4-5 hours, and `ts` is part of the fills table's natural key (`store.py`), so a
    warm-up replay after the change would stop collapsing against what is already recorded
    and would double the position history. The bar's stamp is left exactly where the cache
    says it belongs and the clock the question is asked in is moved instead.

    Identity for the three UTC-cached classes and for a symbol no class claims, so
    commodities, crypto and futures read exactly as they did before.
    """
    now = pd.Timestamp.now(tz="UTC")
    cls = class_of(symbol)
    if cls is None:
        return now.tz_localize(None)
    return now.tz_convert(paper_config.cache_tz(cls)).tz_localize(None)


def _without_forming(df: pd.DataFrame, symbol: str, timeframe: str) -> pd.DataFrame:
    """Discard the newest row unless a full interval has elapsed since it opened.

    The single most important line in this file, in function form so the derived path
    below cannot grow a second copy of it that drifts.

    **`now` is read on the bar's own clock, and only for the intraday sizes.** That is the
    fix for the equity look-ahead described in `_now_in_cache_clock`, and the `1d`
    exclusion is not a shortcut — a daily stamp is a DATE, not a wall-clock instant, so no
    class's intraday zone applies to it. Reading `now` in ET for a daily equity bar would
    also delay it by 4-5 hours past the once-a-day poll and its retry window
    (`td_nautilus._poll`), so the bar would never be delivered at all: the guard would go
    from never firing to always firing, which is the other way to lose a session.
    """
    if not len(df):
        return df
    interval_end = df.index[-1] + INTERVALS[timeframe][1]
    if interval_end.tzinfo is not None:
        now = pd.Timestamp.now(tz="UTC")
    elif paper_config.TIMEFRAMES[timeframe]["intraday"]:
        now = _now_in_cache_clock(symbol)
    else:
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    return df.iloc[:-1] if now < interval_end else df


# Which timeframe a cell has to be BUILT from rather than fetched, because a restamp does
# not preserve the vendor's grid.
#
# **A whole-hour shift moves a bar's LABEL; it only moves the GRID when the bar is wider
# than an hour.** `1m`..`1h` cover exactly the same windows before and after the shift, so
# they are fetched and relabelled. `4h` does not: Sydney is UTC+10/+11, `10 % 4 == 2` and
# `11 % 4 == 3`, so a restamped 4h commodity bar lands at 03:00/07:00/11:00/... UTC — not
# the windows `data/commodities/4h` holds, and not even the same windows as itself across a
# DST change.
#
# **This is the live half of a fix that had only been applied to the cache.**
# `migrate_cache_clock.py` rebuilt the commodity 4h cache from the corrected 1h onto the
# real UTC grid; the desk went on fetching 4h straight from the vendor, so from the day
# that migration shipped the live commodity 4h books were trading bars that **do not exist
# in the research sheet they select their rules from** — measured on `results/paper.db`,
# `00:commodities-4h-*` filled at 11:00 and 15:00 UTC, which is `hour % 4 == 3`, the Sydney
# grid. Same source, same aggregation, same grid on both sides is the only way the forward
# record means what the sheet means.
#
# `1d` is deliberately absent and must stay absent: a daily commodity bar is a third
# convention again — the vendor's own roll-up on a fixed 21:00 UTC boundary, which
# reproduces the daily close to ~2 bp — and a date is not a time.
_DERIVE_SOURCE = "1h"


def _minutes(timeframe: str) -> int:
    return int(timeframe[:-1]) * (60 if timeframe.endswith("h") else 1)


def derived_from(symbol: str, timeframe: str) -> str | None:
    """The timeframe this cell must be built from, or None to ask the vendor directly.

    Arithmetic rather than a list, so a timeframe added later answers for itself. It is
    the same condition as `migrate_cache_clock.relabel_safe`, inverted: a size that divides
    an hour survives a whole-hour restamp, and a size that does not has to be rebuilt.
    """
    if not paper_config.TIMEFRAMES[timeframe]["intraday"]:
        return None                       # a daily stamp is a date; no intraday zone applies
    cls = class_of(symbol)
    if cls is None:
        return None                       # unknown clock: the stamps are left alone anyway
    if paper_config.vendor_tz(cls) == paper_config.cache_tz(cls):
        return None                       # nothing is restamped, so nothing moves off grid
    if 60 % _minutes(timeframe) == 0:
        return None
    return _DERIVE_SOURCE


def _pinned_country(symbol: str) -> bool:
    """Do this symbol's bars have to come from a US listing?

    Equities and ETFs only, which is the same set `td_loader.US_LISTED_CLASSES` names. A
    pair (`BTC/USD`, `XAU/USD`) has no country and pinning one returns nothing; a CME
    future never reaches this module at all.

    An UNKNOWN symbol is pinned too, and that is the deliberate half: an open registration
    is admitted by `symbol_resolve`, which only ever admits equities and ETFs it proved are
    US-listed, plus pairs — and a pair is excluded by the separator test above it. So the
    default that costs nothing when wrong is the strict one.
    """
    if "/" in symbol:
        return False
    try:
        return paper_config.class_of(symbol) in ("us_stocks", "us_etfs")
    except SystemExit:
        # Not on a leg the desk holds — an open symbol resolved at runtime. Pin it.
        return True


def _fetch_raw(symbol: str, timeframe: str, n: int) -> pd.DataFrame:
    """One `/time_series` call, restamped onto the cache clock. No forming-bar test."""
    interval, _ = INTERVALS[timeframe]
    params = {
        "symbol": symbol, "interval": interval, "outputsize": min(n + 2, OUTPUT_SIZE),
        "adjust": "all", "order": "ASC", "apikey": api_key(),
    }
    # **`country` is pinned on equities and ETFs, exactly as `td_loader._request` does.**
    #
    # The root `CLAUDE.md` calls that the fix AT THE SOURCE, and until 2026-08-28 the live
    # bar path did not have it — only the fetch that fills the cache did. Unpinned, Twelve
    # Data does not answer "no" for a ticker with no US listing; it returns SOMEBODY ELSE,
    # as a full, internally consistent series that passes every bar-level check. Probed:
    # `CTRA` unpinned comes back on `IDX` in `IDR` — the Indonesian namesake that once
    # ranked as the 3rd largest US stock here.
    #
    # It was LATENT rather than live, and the distinction is worth keeping: the three known
    # impostors are already out of `paper_config.UNIVERSE`, and pinned-vs-unpinned was
    # verified identical on 16 universe names with the pin breaking none of 45. The hole was
    # the OPEN path — `symbol_resolve` proves a symbol's identity WITH the pin and the bars
    # were then fetched WITHOUT it, so a name with both a US and a foreign listing could be
    # admitted as one and fed as the other. Sampling cannot close that; the pin can.
    if _pinned_country(symbol):
        params["country"] = "United States"
    r = requests.get(f"{BASE_URL}/time_series", timeout=60, params=params)
    if r.status_code != 200:
        # NOT `raise_for_status()`, and the reason is a leak I put here myself and then
        # measured: `requests` builds its message from the full URL, so the exception text
        # reads `...&apikey=<the key>&country=United+States`. That string reaches the desk
        # log and, for an open registration, the `reason` column on the manager console.
        # `symbol_resolve` had the identical defect and scrubs it; this path must too.
        #
        # A 404 here is also the ORDINARY answer now rather than a transport failure: with
        # `country` pinned, a ticker with no US listing is refused by the vendor instead of
        # being served as a foreign namesake. So it is worth a sentence that says which of
        # those happened.
        detail = "no US listing for this symbol" if r.status_code == 404 else                  f"HTTP {r.status_code}"
        raise RuntimeError(f"{symbol} {timeframe}: {detail} "
                           f"(Twelve Data {'refused the pinned request' if _pinned_country(symbol) else 'refused the request'})")
    payload = r.json()
    if payload.get("status") == "error" or "values" not in payload:
        raise RuntimeError(f"{symbol} {timeframe}: {payload}")

    df = _to_frame(payload["values"])
    # Restamped BEFORE the forming-bar test, not after: the test compares the bar's open
    # against `now`, so it is only meaningful once both are on the same clock. This is the
    # line that was discarding every commodity bar.
    if len(df) and paper_config.TIMEFRAMES[timeframe]["intraday"]:
        df.index = _to_cache_clock(df.index, symbol)
    return df


def _fetch_derived(symbol: str, timeframe: str, source: str, n: int,
                   drop_forming: bool) -> pd.DataFrame:
    """Build `timeframe` bars from `source` bars, on the cache's own grid.

    Three things are load-bearing:

    **The aggregation is `resample_intraday.resample_frame`, imported rather than
    reimplemented.** That is the only place in the repo 2m/3m/4h are ever built, and it is
    what wrote the commodity 4h cache this has to agree with. A second copy of
    `label="left", closed="left", origin="start_day"` here is exactly how the live desk and
    the sheet would drift apart again, quietly, in six months.

    **The source's own forming bar is always dropped**, whatever the caller asked for: a
    bucket assembled from an unfinished hour carries a close that has not happened, which
    is the same look-ahead one grid finer.

    **A bucket is settled when the SOURCE has settled through its end**, not merely when
    the wall clock has passed it. At the 4h boundary plus the poll lag the vendor has
    usually published the bucket's last hour and usually is not good enough — a bucket
    published one hour short has the wrong high, low and close and nothing downstream can
    tell. The wall-clock clause behind it is the escape hatch for a source that will never
    arrive (the market shut mid-bucket, which is every Friday on spot metals), so a bucket
    is held for at most one extra source interval rather than forever.
    """
    import resample_intraday                                          # noqa: PLC0415

    step = INTERVALS[timeframe][1] // INTERVALS[source][1]
    base = _without_forming(_fetch_raw(symbol, source, n * step + step), symbol, source)
    if not len(base):
        return base
    out = resample_intraday.resample_frame(base, _minutes(timeframe))
    if drop_forming and len(out):
        bucket_end = out.index[-1] + INTERVALS[timeframe][1]
        covered_to = base.index[-1] + INTERVALS[source][1]
        stale_enough = _now_in_cache_clock(symbol) >= bucket_end + INTERVALS[source][1]
        if covered_to < bucket_end and not stale_enough:
            out = out.iloc[:-1]
    return out.tail(n)


def fetch_bars(symbol: str, timeframe: str, n: int = 1500,
               drop_forming: bool = True) -> pd.DataFrame:
    """The last `n` CLOSED bars for one symbol, on that symbol's CACHE clock and grid.

    With `drop_forming` the newest row is discarded unless a full interval has elapsed
    since it opened. Trading the forming bar would use a close that has not happened yet.

    The index is restamped onto the clock the backtest cache for that class is on, whatever
    clock the vendor answered in — see `_to_cache_clock`. That is what keeps the live
    record comparable to the sheet the rule was selected from, and it is what puts the
    commodity legs back on UTC, where the forming-bar guard here, `ts_event` in
    `td_nautilus` and `_seconds_to_next_close`'s modular arithmetic all already assumed
    they were.

    A restamp fixes a LABEL and not a GRID, so a cell `derived_from` names is built from a
    finer timeframe instead of fetched — see there. Today that is commodity `4h` and
    nothing else.

    **The warm-up is shallower on a derived cell and that is the one cost.** A single
    vendor call is capped at `OUTPUT_SIZE` (5,000 bars), so 4h built from 1h tops out at
    1,250 bars against the 1,500 `DEFAULT_WINDOW_BARS` asks for. That still clears
    `paper_config.MEASURED_WINDOW_BARS` (1,000), which is the number `parity_live.py`
    measured as the point a recursive rule reproduces the full series, so the signal is
    unaffected; it is written down here because "asked for 1,500, got 1,250" is otherwise
    the kind of silent shortfall this file exists to prevent.
    """
    source = derived_from(symbol, timeframe)
    if source is not None:
        return _fetch_derived(symbol, timeframe, source, n, drop_forming)

    df = _fetch_raw(symbol, timeframe, n)
    if drop_forming:
        df = _without_forming(df, symbol, timeframe)
    return df.tail(n)


def latest_closed_bar(symbol: str, timeframe: str) -> pd.Series | None:
    df = fetch_bars(symbol, timeframe, n=1)
    return df.iloc[-1] if len(df) else None


def return_between(symbol: str, timeframe: str, start: datetime) -> float | None:
    """Percent move in `symbol` from the last bar at or before `start` to the latest close.

    This is what buy-and-hold did while the paper desk was stopped. The strategy earned 0
    over that window because it held nothing; the benchmark did not, and pretending
    otherwise would flatter every strategy through a drawdown it simply was not present
    for. Returns None when the window cannot be measured — the caller stores that as an
    unknown gap rather than as a zero.
    """
    _, span = INTERVALS[timeframe]
    elapsed = datetime.now(timezone.utc) - start
    bars = int(elapsed / span) + 5
    if bars < 2:
        return None
    df = fetch_bars(symbol, timeframe, n=min(bars, 5000))
    if df is None or len(df) < 2:
        return None
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    prior = df[idx <= start]
    if prior.empty:
        return None
    first = float(prior["Close"].iloc[-1])
    last = float(df["Close"].iloc[-1])
    if first <= 0:
        return None
    return round((last / first - 1.0) * 100.0, 6)


def is_market_open(symbol: str) -> bool | None:
    """Vendor's own session flag. None when it does not report one (e.g. crypto)."""
    r = requests.get(f"{BASE_URL}/quote", timeout=30,
                     params={"symbol": symbol, "apikey": api_key()})
    r.raise_for_status()
    return r.json().get("is_market_open")


# How much of one `/price` request the symbol list may fill. The vendor puts the batch in
# the QUERY STRING, so the ceiling is its gateway's URL limit and not anything it
# documents: at 125 symbols the desk's request was ~1.1 kB of URL and came back
# `414 URI Too Long` every single time. Both guards are needed — a count alone still
# overflows on crypto pairs, which cost 11 characters each once `/` is percent-encoded,
# and a length alone would send one enormous request the day the vendor raises the limit.
MAX_PRICE_SYMBOLS = 50
MAX_PRICE_CHARS = 400


def _price_chunks(symbols: list[str]) -> list[list[str]]:
    """Split a symbol list into requests that will fit in a URL."""
    chunks: list[list[str]] = []
    run: list[str] = []
    run_len = 0
    for sym in symbols:
        cost = len(sym) * 3 + 3               # worst-case percent-encoding, plus a comma
        if run and (len(run) >= MAX_PRICE_SYMBOLS or run_len + cost > MAX_PRICE_CHARS):
            chunks.append(run)
            run, run_len = [], 0
        run.append(sym)
        run_len += cost
    if run:
        chunks.append(run)
    return chunks


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Current price for many symbols, in as few calls as will fit in a URL.

    `/price` is batched — a comma-separated list returns a dict keyed by symbol. A
    single-symbol request returns the bare object instead of a dict of them, so both shapes
    are handled.

    **It is batched into the URL, which is why this chunks.** The docstring here used to
    promise "one call", and that held while the desk marked 33 instruments. The point-in-time
    top 100 took it to ~125, the request grew past the vendor's URL limit, and mark-to-market
    then failed with `414 URI Too Long` on every pass — 1,035 failures and no successes in a
    day, while the desk went on filling orders against prices it could no longer refresh. A
    batch endpoint has a size, and nothing here was bounding it.

    **A chunk that fails does not take the others down with it.** Partial marks are the
    point: one unpriceable symbol used to mean the whole book went unpriced. Only a total
    failure raises, so the caller's "will retry" still fires for a real outage rather than
    for one bad ticker.

    This exists for mark-to-market only, never for signals. Positions are decided on closed
    bars; an intraday price is for showing what the open position is worth right now, and
    feeding it to a rule would trade a bar that has not finished forming.
    """
    if not symbols:
        return {}
    out: dict[str, float] = {}
    failures: list[Exception] = []
    chunks = _price_chunks(symbols)
    for chunk in chunks:
        try:
            r = requests.get(f"{BASE_URL}/price", timeout=30,
                             params={"symbol": ",".join(chunk), "apikey": api_key()})
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:               # noqa: BLE001 - reported below, not swallowed
            failures.append(exc)
            continue
        if "price" in payload:                 # single symbol: bare object
            payload = {chunk[0]: payload}
        for sym, v in payload.items():
            try:
                out[sym] = float(v["price"])
            except (TypeError, ValueError, KeyError):
                continue
    if failures and len(failures) == len(chunks):
        raise failures[0]
    if failures:
        print(f"fetch_prices: {len(failures)} of {len(chunks)} chunks failed "
              f"({failures[0]}); marked {len(out)} of {len(symbols)} symbols", flush=True)
    return out


# --------------------------------------------------------------------- Nautilus adapter

def build_data_client(*args, **kwargs):
    """Import the Nautilus client lazily.

    `nautilus_trader` lives in its own venv (`.venv-nautilus`) because it pins numpy and
    pyarrow against the rest of the repo. Importing it at module scope would make this
    file unusable from the research venv, where `fetch_bars` is perfectly useful on its
    own — `parity_live.py` and the smoke test below both run without Nautilus installed.
    """
    from td_nautilus import TwelveDataLiveClient      # noqa: PLC0415
    return TwelveDataLiveClient(*args, **kwargs)


def _smoke() -> None:
    """Prove the vendor path end to end without touching Nautilus."""
    print(f"warmup window in use: {paper_config.DEFAULT_WINDOW_BARS} bars "
          f"(measured worst case {paper_config.MEASURED_WINDOW_BARS})")
    for tf in paper_config.FORWARD_TIMEFRAMES:
        for sym in ["SOXL", "TQQQ", "BTC/USD"]:
            try:
                df = fetch_bars(sym, tf, n=paper_config.DEFAULT_WINDOW_BARS)
                raw = fetch_bars(sym, tf, n=5, drop_forming=False)
                dropped = len(raw) and (len(df) == 0 or raw.index[-1] > df.index[-1])
                print(f"  {sym:9s} {tf:3s} {len(df):5d} closed bars | "
                      f"last close {df.index[-1]} @ {df['Close'].iloc[-1]:.4f}"
                      f"{'  [forming bar dropped]' if dropped else ''}")
            except Exception as exc:
                print(f"  {sym:9s} {tf:3s} FAILED: {exc}")


if __name__ == "__main__":
    _smoke()


# Twelve Data accepts a comma-separated symbol list on `/time_series` and answers with a
# dict keyed by symbol. Chunked below this, because a very long list makes one request
# whose failure loses every symbol in it — and because the vendor caps the list length.
BATCH_SYMBOLS = 50


def fetch_bars_many(symbols: list[str], timeframe: str, n: int = 1500,
                    drop_forming: bool = True,
                    country: str | None = None) -> dict[str, pd.DataFrame]:
    """The last `n` closed bars for MANY symbols, in as few requests as possible.

    `fetch_bars` asks for one symbol per request, which is fine for a 33-instrument desk
    and stops being fine at a hundred: every subscription polls at the same bar close, so
    a hundred names is a hundred simultaneous requests in the second after the bell.

    Credits are counted per symbol either way — batching does not make the data cheaper —
    but it collapses the request COUNT, which is what a rate limiter measures and what
    turns a burst into a queue of failures.

    A symbol that errors is omitted from the result rather than raising, because one
    delisted or mistyped name must not cost the other ninety-nine their bars. The caller
    sees a short dict and can say which are missing.

    **Nothing in the repo calls this today, and it must not be wired up as it stands.** It
    predates `config.INTRADAY_CLOCK`: it neither restamps onto the cache clock nor asks the
    bar's own clock for `now`, so it carries BOTH defects `fetch_bars` was fixed for — the
    commodity legs one bar behind, and the intraday equity legs keeping the forming bar. It
    also does not know about `derived_from`, so it would hand back commodity 4h on the
    vendor's Sydney grid. Route it through `_fetch_raw`, `_without_forming` and
    `derived_from` before using it for anything.
    """
    interval, duration = INTERVALS[timeframe]
    out: dict[str, pd.DataFrame] = {}
    now = datetime.now(timezone.utc)

    for i in range(0, len(symbols), BATCH_SYMBOLS):
        chunk = symbols[i:i + BATCH_SYMBOLS]
        params = {
            "symbol": ",".join(chunk), "interval": interval,
            "outputsize": min(n + 2, OUTPUT_SIZE), "adjust": "all",
            "order": "ASC", "apikey": api_key(),
        }
        if country:
            params["country"] = country
        r = requests.get(f"{BASE_URL}/time_series", timeout=120, params=params)
        r.raise_for_status()
        payload = r.json()

        # One symbol comes back as the bare object, several as a dict keyed by symbol —
        # the same asymmetry `fetch_prices` handles, and the same trap: a one-name chunk
        # would otherwise be read as a dict of symbols called "meta" and "values".
        if "values" in payload or payload.get("status") == "error":
            payload = {chunk[0]: payload}

        for symbol in chunk:
            block = payload.get(symbol)
            if not isinstance(block, dict) or "values" not in block:
                continue
            df = _to_frame(block["values"])
            if drop_forming and len(df):
                last_open = df.index[-1]
                if last_open.tzinfo is None:
                    last_open = last_open.tz_localize("UTC")
                if now < last_open + duration:
                    df = df.iloc[:-1]
            out[symbol] = df.tail(n)
    return out
