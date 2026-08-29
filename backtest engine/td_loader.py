"""Fetch OHLCV for both asset classes at all seven timeframes from Twelve Data.

Descends from `../top 20 stocks/td_loader.py` with three changes that matter:

* **Windows are per (class, timeframe).** Crypto trades 24/7, so a 5-minute window that
  is safely under the 5000-bar cap for a US equity holds ~3.7x too many crypto bars.
* **A full window is split, not trusted.** If a response comes back at exactly
  `OUTPUT_SIZE` rows it has almost certainly been truncated, and a truncated window is
  a silent hole in the middle of a series. The window is bisected and re-fetched
  instead. This makes `window_days` a performance knob rather than a correctness one.
* **Crypto symbols carry a slash**, which is a path separator, so cache files use
  `config.safe_symbol` while every in-memory key stays the real symbol.

**Intraday bars are restamped onto the cache's clock before they are written.** The vendor
returns a naive wall clock and declares which zone it is only sometimes — and for
commodities it declared `null` while stamping `Australia/Sydney`. `config.INTRADAY_CLOCK`
is the per-class declaration and `to_cache_clock_frame` below applies it, on fetch, so the
parquet on disk holds one convention per class instead of whatever the vendor felt like.

Run::

    python td_loader.py                      # everything (long: ~1h, ~13k credits)
    python td_loader.py --class crypto       # one class
    python td_loader.py --tf 1d 4h           # one or more timeframes
    python td_loader.py --class crypto --tf 1d --symbols BTC/USD
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from tqdm import tqdm

from config import (BACKTEST_START, CLASSES, DATA_DIR, ENV_FILE, TIMEFRAMES, cache_dir,
                    dst_hazard, safe_symbol, to_cache_clock, vendor_tz, window_spec)

BASE_URL = "https://api.twelvedata.com/time_series"
MAX_SYMBOLS_PER_REQUEST = 20
# Smaller than the cap for intraday: 20 symbols x 5000 bars is a ~15 MB JSON body, and
# parsing one oversized response costs more than issuing two clean requests.
BATCH_SIZE = 5
OUTPUT_SIZE = 5000
CREDITS_PER_MINUTE = 550
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

_credit_log: deque[float] = deque()


def api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TWELVEDATA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"No Twelve Data API key (env TWELVEDATA_API_KEY or {ENV_FILE})")


def _spend_credits(n: int) -> None:
    """Block until `n` more credits fit inside the trailing 60-second window."""
    while True:
        now = time.monotonic()
        while _credit_log and now - _credit_log[0] > 60.0:
            _credit_log.popleft()
        if len(_credit_log) + n <= CREDITS_PER_MINUTE:
            break
        time.sleep(max(0.1, 60.0 - (now - _credit_log[0])))
    stamp = time.monotonic()
    _credit_log.extend([stamp] * n)


def _api_symbol(symbol: str) -> str:
    """Project spelling -> Twelve Data spelling (`BRK-B` -> `BRK.B`).

    Crypto pairs already use the vendor's own `BASE/QUOTE` form and pass through.
    """
    return symbol if "/" in symbol else symbol.replace("-", ".")


def _to_frame(values: list[dict]) -> pd.DataFrame:
    """One symbol's `values` array -> the project's standard OHLCV frame.

    Twelve Data omits `volume` entirely for crypto pairs — the key is absent, not zero.
    It is carried as NaN rather than filled with 0.0 on purpose: a volume-consuming rule
    (AD, OBV, MFI, ...) fed zeros produces a flat position and would rank as a rule that
    merely *does nothing*, which is indistinguishable on a leaderboard from a real
    result. NaN makes it fail loudly instead, and `sweep.py` skips those rules for any
    class whose data has no volume rather than reporting them as duds.
    """
    df = pd.DataFrame(values)
    df["Date"] = pd.to_datetime(df["datetime"])
    df = df.set_index("Date").sort_index()
    cols = {
        "Open": pd.to_numeric(df["open"]),
        "High": pd.to_numeric(df["high"]),
        "Low": pd.to_numeric(df["low"]),
        "Close": pd.to_numeric(df["close"]),
    }
    if "volume" in df.columns:
        # float64, not int64: crypto volume would be fractional if it were served, and
        # nothing downstream reads Volume as an integer.
        cols["Volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("float64")
    else:
        cols["Volume"] = pd.Series(float("nan"), index=df.index, dtype="float64")
    out = pd.DataFrame(cols)
    # Drop on price columns only — dropping on Volume would empty every crypto frame.
    return out.dropna(subset=["Open", "High", "Low", "Close"])


# Classes whose symbols are US-listed, and which therefore pin `country` on every request.
#
# A BARE SYMBOL IS NOT AN IDENTITY. Twelve Data resolves one against every venue it
# carries and answers with whatever instrument wears those letters, anywhere on earth.
# Left unpinned this does not fail loudly — it returns a full, internally consistent,
# structurally perfect series belonging to a different company on a different continent in
# a different currency, and every downstream check passes it:
#
#     CTRA  -> Ciputra Development Tbk PT      Indonesia Stock Exchange, rupiah
#     STJ   -> St. James's Place Plc           LSE, pence
#     K     -> Kinross Gold Corporation        TSX, Canadian dollars
#     X     -> TMX Group Limited               TSX
#     HSP   -> Hargreaves Services Plc         LSE
#
# Measured 2026-08-12 across the 739 cached us_stocks 1d series, **85 of them** were a
# foreign namesake for their entire length, and four of those had won a slot in the
# point-in-time top-100 universe on the strength of a rupiah-denominated dollar volume.
#
# `country=United States` fixes it at the source. The vendor then returns `status: error`
# for a ticker it has no US listing for, instead of silently substituting one — verified on
# a mixed NASDAQ/NYSE batch, so it costs nothing and works with the batching above.
#
# Crypto and commodities are FX-style pairs with no country, and passing one there returns
# nothing at all, so the pin is per class rather than global.
US_LISTED_CLASSES = ("us_stocks", "us_etfs")


def _request(symbols: list[str], interval: str, start: str, end: str,
             depth: int = 0, asset_class: str | None = None) -> dict[str, pd.DataFrame]:
    """One batched request, bisecting on symbol-count overflow or a full response."""
    if len(symbols) > MAX_SYMBOLS_PER_REQUEST:
        mid = len(symbols) // 2
        out = _request(symbols[:mid], interval, start, end, depth, asset_class)
        out.update(_request(symbols[mid:], interval, start, end, depth, asset_class))
        return out

    api_names = {_api_symbol(s): s for s in symbols}
    params = {
        "symbol": ",".join(api_names), "interval": interval,
        "start_date": start, "end_date": end, "outputsize": OUTPUT_SIZE,
        "adjust": "all", "order": "ASC", "apikey": api_key(),
    }
    if asset_class in US_LISTED_CLASSES:
        params["country"] = "United States"
    _spend_credits(len(symbols))
    response = requests.get(BASE_URL, params=params, timeout=180)
    if response.status_code == 414 and len(symbols) > 1:
        mid = len(symbols) // 2
        out = _request(symbols[:mid], interval, start, end, depth, asset_class)
        out.update(_request(symbols[mid:], interval, start, end, depth, asset_class))
        return out
    response.raise_for_status()
    payload = response.json()

    if len(api_names) == 1:
        payload = {next(iter(api_names)): payload}

    out: dict[str, pd.DataFrame] = {}
    saturated: list[str] = []
    for api_name, symbol in api_names.items():
        entry = payload.get(api_name)
        # "No data on the specified dates" is expected when a window lands before a
        # symbol's listing or over a market closure — non-fatal, just no rows.
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        values = entry.get("values")
        if not values:
            continue
        if len(values) >= OUTPUT_SIZE:
            saturated.append(symbol)
            continue
        frame = _to_frame(values)
        if not frame.empty:
            out[symbol] = frame

    # A response at exactly the cap has almost certainly been truncated, which would
    # leave an invisible hole mid-series. Halve the window and refetch those symbols.
    if saturated:
        if depth >= 6:
            raise RuntimeError(
                f"window {start}..{end} still saturated for {saturated} after "
                f"{depth} bisections - lower window_days for this class/timeframe")
        lo = datetime.strptime(start, "%Y-%m-%d")
        hi = datetime.strptime(end, "%Y-%m-%d")
        if (hi - lo).days <= 1:
            raise RuntimeError(f"single-day window {start} exceeds {OUTPUT_SIZE} bars "
                               f"for {saturated} - interval too fine to page by date")
        mid = (lo + (hi - lo) / 2).strftime("%Y-%m-%d")
        first = _request(saturated, interval, start, mid, depth + 1, asset_class)
        second = _request(saturated, interval, mid, end, depth + 1, asset_class)
        for symbol in saturated:
            parts = [p for p in (first.get(symbol), second.get(symbol)) if p is not None]
            if parts:
                out[symbol] = pd.concat(parts)
    return out


def _windows(start: str, window_days: int) -> list[tuple[str, str]]:
    """Consecutive [start, end) date windows from `start` up to tomorrow."""
    begin = datetime.strptime(start, "%Y-%m-%d")
    today = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    out = []
    while begin < today:
        end = min(begin + timedelta(days=window_days), today)
        out.append((begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        begin = end
    return out


def fetch(asset_class: str, timeframe: str,
          symbols: list[str] | None = None) -> dict[str, int]:
    spec = CLASSES[asset_class]
    # A class can name its own vendor, and one does: `cme_futures` comes from Databento
    # via `db_loader.py`. Refusing here rather than in `main` matters, because Twelve
    # Data would not fail on a CME root — it would return the equity that wears the same
    # letters, in full, and every downstream check would pass it. `CL` is crude oil to
    # this class and Colgate-Palmolive to the vendor.
    source = spec.get("source", "twelvedata")
    if source != "twelvedata":
        raise ValueError(
            f"{asset_class} is sourced from {source}, not Twelve Data. Fetch it with "
            f"`python db_loader.py --class {asset_class}` instead.")
    win = window_spec(asset_class, timeframe)
    interval = TIMEFRAMES[timeframe]["interval"]
    if interval is None:
        # Same refusal shape as the vendor check above, for the same reason: Twelve Data
        # serves no 2min/3min product, and asking anyway is how a wrong-but-plausible
        # series ends up in the cache. These bars are derived, never fetched.
        raise ValueError(
            f"{timeframe} is not a vendor interval. Resample it from the cached 1m with "
            f"`python resample_intraday.py --class {asset_class} --tf {timeframe}`.")

    wanted = list(symbols) if symbols else list(spec["symbols"])
    bench = spec.get("benchmark")
    if not symbols and bench and bench not in wanted:
        wanted.append(bench)

    out_dir = cache_dir(asset_class, timeframe)
    out_dir.mkdir(parents=True, exist_ok=True)

    windows = _windows(win["start"], win["window_days"])
    batches = [wanted[i:i + BATCH_SIZE] for i in range(0, len(wanted), BATCH_SIZE)]
    collected: dict[str, list[pd.DataFrame]] = {s: [] for s in wanted}

    jobs = [(b, w) for b in batches for w in windows]
    label = f"{asset_class}/{timeframe} ({len(windows)}w x {len(batches)}b)"
    for batch, (start, end) in tqdm(jobs, desc=label):
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                for symbol, frame in _request(batch, interval, start, end,
                                              asset_class=asset_class).items():
                    collected[symbol].append(frame)
                break
            except Exception as exc:
                if attempt == RETRY_ATTEMPTS:
                    print(f"  window {start}..{end} failed for {batch}: {exc}")
                else:
                    time.sleep(RETRY_DELAY_SEC)

    counts = {}
    hazard = {"ambiguous": 0, "nonexistent": 0}
    for symbol, frames in collected.items():
        if not frames:
            print(f"  {symbol}: no data")
            continue
        # Windows are half-open but the API is inclusive at both ends, so adjacent
        # windows repeat a boundary bar.
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        if TIMEFRAMES[timeframe]["intraday"]:
            df = to_cache_clock_frame(df, asset_class, hazard)
        df.to_parquet(out_dir / f"{safe_symbol(symbol)}.parquet")
        counts[symbol] = len(df)
    if hazard["ambiguous"] or hazard["nonexistent"]:
        print(f"  {asset_class}/{timeframe}: {hazard['ambiguous']} ambiguous and "
              f"{hazard['nonexistent']} nonexistent stamps in "
              f"{vendor_tz(asset_class)} - see config.AMBIGUOUS_POLICY")
    return counts


def to_cache_clock_frame(df: pd.DataFrame, asset_class: str,
                         hazard: dict[str, int] | None = None) -> pd.DataFrame:
    """Restamp one intraday frame from the vendor's wall clock onto the cache's.

    **This runs on FETCH, not on load, and that is the whole point.** The vendor stamps
    commodity intraday bars in `Australia/Sydney` and declares nothing — see
    `config.INTRADAY_CLOCK` for how that was measured. Converting at load time would
    leave a cache that lies, and this repo does not read its cache through one door only:
    `check_data`, `resample_intraday`, `verify_intraday_vs_daily` and every ad-hoc
    reconciliation open the parquet directly. One convention on disk, decided once here.

    Identity for the four classes whose two clocks already agree, so it is safe to call
    on every intraday fetch rather than behind an `if asset_class == ...`.

    Re-sorted and re-deduplicated after the shift because a DST transition is the one
    place a monotonic naive index can come back out of order or collide — see
    `config.AMBIGUOUS_POLICY`. The counts go to `hazard` so the caller can report them;
    silently absorbing them would hide genuine information loss in the vendor's stamping.
    """
    if hazard is not None:
        for k, v in dst_hazard(df.index, asset_class).items():
            hazard[k] += v
    out = df.copy()
    out.index = to_cache_clock(out.index, asset_class)
    out.index.name = df.index.name
    out = out.sort_index()
    return out[~out.index.duplicated(keep="first")]


_QUARANTINE_CACHE: dict | None = None


def quarantined(asset_class: str, timeframe: str) -> set[str]:
    """Symbols `check_data.py` judged unusable for this (class, timeframe).

    Read here rather than filtered by each caller because this function is the single
    door every stage goes through, and a symbol that must not be swept must not be swept
    by *any* of them. `MRO` survives the OHLC checks with a +4,600% bar and a
    buy-and-hold equity of 3.2e17; one stage forgetting to exclude it is one contaminated
    sheet, and this repo has already published two results it had to retract.
    """
    global _QUARANTINE_CACHE
    if _QUARANTINE_CACHE is None:
        path = DATA_DIR / "reference" / "quarantine.csv"
        _QUARANTINE_CACHE = {}
        if path.exists():
            try:
                q = pd.read_csv(path)
                # Indexed by column name, not by itertuples position: `class` is a Python
                # keyword, so pandas renames that attribute to `_1` and the positional
                # form silently reads the wrong field the moment a column is added.
                for cls, tf, sym in zip(q["class"], q["timeframe"], q["symbol"]):
                    _QUARANTINE_CACHE.setdefault((cls, tf), set()).add(sym)
            except Exception:
                _QUARANTINE_CACHE = {}
    return _QUARANTINE_CACHE.get((asset_class, timeframe), set())


def load(asset_class: str, timeframe: str, symbols: list[str] | None = None,
         skip_quarantined: bool = True) -> dict[str, pd.DataFrame]:
    """Cache-only. Never downloads — call `fetch` first.

    `skip_quarantined=False` exists for `check_data.py`, which has to be able to see the
    bars it is judging. Nothing else should pass it.
    """
    out_dir = cache_dir(asset_class, timeframe)
    wanted = symbols if symbols is not None else CLASSES[asset_class]["symbols"]
    skip = quarantined(asset_class, timeframe) if skip_quarantined else set()
    # `config.BACKTEST_START` is applied HERE and nowhere else. Every stage in the repo
    # -- sweeps, walk-forward, variants, the portfolio book, parity, the paper desk --
    # reaches its bars through this one function, so one cut here is the whole pipeline
    # agreeing on a start date. Doing it per-stage is how two sheets end up spanning
    # different windows and being compared anyway.
    #
    # `check_data.py` is the deliberate exception: it passes `skip_quarantined=False` to
    # judge bars, and it must see the FULL series to do that. Truncating its view would
    # let a pre-2000 decimal spike sit unrepaired in the cache forever, invisible right up
    # until someone moves the start date back.
    cut = pd.Timestamp(BACKTEST_START) if (BACKTEST_START and skip_quarantined) else None
    spans = span_for(asset_class) if skip_quarantined else {}
    data = {}
    for symbol in wanted:
        if symbol in skip:
            continue
        path = out_dir / f"{safe_symbol(symbol)}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            if cut is not None:
                df = df.loc[df.index >= cut]
            span = spans.get(symbol)
            if span is not None:
                begin, end = span
                if begin is not None:
                    df = df.loc[df.index >= begin]
                if end is not None:
                    df = df.loc[df.index <= end]
            if df.empty:
                continue
            data[symbol] = df
    return data


def span_for(asset_class: str) -> dict[str, tuple]:
    """`symbol -> (first held date, last held date)` for a class, or `{}` if it has none.

    Two classes carry a span and they answer the same question from different records:

    * `us_stocks` — `membership_span()`, from the point-in-time top-100 record. A name is
      held on the dates it was one of the hundred largest.
    * `us_etfs` — `etf_entry_span()`, from the liquidity screen. A fund is held from the
      date it became buyable.

    * `commodities` — `commodity_entry_span()`, from the fabricated-Open screen. A metal
      is held from the date its Open stops being a placeholder.

    The third one does NOT answer a tradability question like the other two, and the
    difference is worth keeping straight: `us_stocks` and `us_etfs` cut bars a rule could
    not have traded, while `commodities` cuts bars whose *prices are not measurements*.
    Gold and silver print an Open exactly equal to the High or the Low on every bar until
    early 2006. Same mechanism, different reason, so it gets its own record rather than
    being folded into the liquidity screen.

    `crypto` has none, and would not benefit: the vendor serves no volume for the class, so
    there is no turnover series to gate an entry date on, and its Open is not synthetic.
    Its screen is at the *name* level only — `universe_screen.py` drops a pair from the
    universe or keeps it whole. Every caveat that implies is on that module's sheet.
    """
    if asset_class == "us_stocks":
        return membership_span()
    if asset_class == "us_etfs":
        return etf_entry_span()
    if asset_class == "commodities":
        return commodity_entry_span()
    return {}


_COMMODITY_ENTRY: dict[str, tuple] | None = None


def commodity_entry_span() -> dict[str, tuple]:
    """`symbol -> (date the Open became a real price, None)`, from the fabricated-Open screen.

    Head cut only. Written by `commodity_entry.py --write` to
    `data/reference/commodity_entry.csv`, which carries the measured reason for each date.

    **Twelve Data serves gold and silver with a synthetic Open for the first half of their
    history** — exactly equal to the High or exactly equal to the Low on 100% of bars, for
    27 years of XAU and 24 of XAG, ending in the same week of February 2006 on both. After
    the break the rate settles at 0-15%, which is what a real Open does on quiet and
    gapping sessions.

    Two things make this worth a cut rather than a caveat. It sits INSIDE the backtest
    window — `BACKTEST_START` is 2000, so six affected years were being scored — and
    `check_data --fix` actively propagates it: an Open outside its own High/Low is a
    malformed bar, so the repair widens the extremes onto the bad number and the result is
    a bar that passes every integrity test with three fabricated fields instead of one.

    `--fill open` is where it bites hardest, which is the fill this repo quotes as its
    honest pessimistic bound.

    Absent file, or a name absent from it, means no cut — the same convention
    `etf_entry_span` and `membership_span` use.
    """
    global _COMMODITY_ENTRY
    if _COMMODITY_ENTRY is None:
        path = DATA_DIR / "reference" / "commodity_entry.csv"
        if not path.exists():
            _COMMODITY_ENTRY = {}
        else:
            df = pd.read_csv(path, parse_dates=["entry"])
            _COMMODITY_ENTRY = {
                str(r.symbol): (r.entry if pd.notna(r.entry) else None, None)
                for r in df.itertuples()
            }
    return _COMMODITY_ENTRY


_ETF_ENTRY: dict[str, tuple] | None = None


def etf_entry_span() -> dict[str, tuple]:
    """`symbol -> (date it became liquid enough to trade, None)`, from the ETF screen.

    Head cut only. Written by `universe_screen.py --write` to
    `data/reference/etf_entry.csv`, which also carries the floor that produced each date.

    **A fund's ticker existing is not the same as the fund being buyable**, and on this
    class the gap is years wide. The nine original sector SPDRs listed in December 1998
    and then traded under $2M/day until roughly 2004 — XLU's worst year is $0.1M/day, XLV
    and XLI and XLB all bottom at $0.3M. A rule scored on those bars is being scored
    against a market that could not have filled it, and the flattery runs the usual
    direction: a thin tape has wider ranges and more reversion in it, which is exactly
    what the oscillator family is paid for.

    No tail cut, and unlike `membership_span` that is not a placeholder for one. A company
    can leave the top 100 while its ticker keeps trading, and `membership_span` truncates
    there because the later bars belong to a name outside the universe. None of these ten
    funds died, shrank out of the basket, or had its ticker recycled; every one turns over
    hundreds of millions a day in its last five years. `universe_screen`'s `dv_last5y`
    column is where that stops being true if it ever does.

    Absent file, or a name absent from it, means no cut — the same convention
    `membership_span` uses, so a fresh clone that has not run the screen yet loads full
    series rather than nothing.
    """
    global _ETF_ENTRY
    if _ETF_ENTRY is None:
        path = DATA_DIR / "reference" / "etf_entry.csv"
        if not path.exists():
            _ETF_ENTRY = {}
        else:
            df = pd.read_csv(path, parse_dates=["entry"])
            _ETF_ENTRY = {
                str(r.symbol): (r.entry if pd.notna(r.entry) else None, None)
                for r in df.itertuples()
            }
    return _ETF_ENTRY


_SPANS: dict[str, tuple] | None = None


def membership_span() -> dict[str, tuple]:
    """`symbol -> (first date it entered the top 100, last date it was in it)`.

    A name still in the universe has `end is None` and is never truncated at the tail; a
    name that has been there since the study opened has `begin is None` at the head.

    **This is a SPAN, not a per-bar mask, and the difference is deliberate.** A name that
    held a slot in 2004, dropped out, and came back in 2015 keeps its 2004-2015 bars here.
    Punching holes in a price series would silently corrupt every indicator that reads
    across one — a 200-day moving average spanning a four-year gap is not a 200-day moving
    average — and the per-asset sheets are asking "how did this rule do on this name",
    which is a question about a continuous series.

    Exact per-bar membership is applied where it actually changes the answer: at BOOK
    level, in `portfolio_wf.membership_mask`, which weights each bar by who was live on it
    and is the only place the universe's size (~100 names, not 216) is load-bearing.

    Two failures this closes, one inherited and one new:

    **Ticker recycling.** `check_data`'s dollar-volume test catches an impostor that is
    THIN — `FL` at $7,494/day. It is blind to one that is FAT: `HSP` turns over $22.6M a
    day because those three letters now belong to a real, liquid company, just not
    Hospira, which was acquired in 2015. (`check_data.wrong_instrument_reason` now catches
    the subset of those the vendor has no US listing for at all, which was 85 names; the
    tail truncation here is what handles the rest.)

    **Size drift.** New with the top-100 universe, and the reason a head cut exists at all.
    NVDA has bars from 1999, but it was not one of the hundred largest US listings until
    much later; scoring a rule on its small-cap decade and calling the result a top-100
    result is exactly the survivorship the point-in-time machinery exists to remove. The
    head cut is what makes this a study of large caps rather than a study of names that
    later became large.

    Names that kept trading after dropping out — AA, GME, RIG — lose their post-exit
    history too, and that is correct rather than collateral.

    **What the head cut costs, stated because it is a real cost and not a rounding one.**
    An indicator needs warmup, so a name entering in 2015 spends its first ~200 bars with
    a NaN signal and contributes nothing over that stretch. Reading the bars *before* the
    entry date would not be look-ahead — that history was genuinely available at the time
    — so in principle the right shape is "load from entry minus a warmup allowance, but
    only SCORE from entry". This loader cannot express that: every bar it returns is a bar
    the stages score, and separating the two needs a scoring mask threaded through
    `sweep`, `walkforward`, `variants`, `prereg` and `strat_wf`. The hard cut is the
    conservative side of that trade — it throws away signal rather than admitting bias —
    and the ~200 bars land at a fresh entrant's start, where `MIN_BARS` is doing the real
    filtering anyway.
    """
    global _SPANS
    if _SPANS is None:
        path = DATA_DIR / "reference" / "top100_membership.csv"
        if not path.exists():
            _SPANS = {}
        else:
            iv = pd.read_csv(path, parse_dates=["start", "end"])
            _SPANS = {}
            for sym, grp in iv.groupby("symbol"):
                begin = grp["start"].min()
                # Any open spell means "still in the universe": take no tail cut at all.
                end = None if grp["end"].isna().any() else grp["end"].max()
                _SPANS[str(sym)] = (begin if pd.notna(begin) else None, end)
    return _SPANS


_EXITS: dict[str, pd.Timestamp] | None = None


def membership_exits() -> dict[str, pd.Timestamp]:
    """`symbol -> last date it was an S&P 500 member`, for names that have left.

    Superseded by `membership_span` for loading — kept because `portfolio_wf`'s
    survivorship stress still reasons about S&P departures specifically, which is a
    different question from leaving the top 100.

    Current members are absent from this map, so they are never truncated.

    This closes the half of the ticker-recycling problem that liquidity cannot reach.
    `check_data`'s dollar-volume test catches an impostor that is THIN — `FL` at $7,494/day.
    It is blind to one that is FAT, and those exist: `HSP` turns over **$22.6M a day**
    because those three letters now belong to a real, liquid company, just not Hospira,
    which was acquired in 2015. On the 4h sheet, which starts in 2019, every HSP bar
    postdates the company by four years and every one of them passed every check.

    The membership record already knows the answer, and the universe already claims to use
    it: departed names are documented as "held only on the dates they were actually index
    members". That was true of `portfolio_wf --pit` and of nothing else, so every other
    stage read bars belonging to whoever holds the ticker now.

    The bite is real and it is meant to be. On us_stocks 1d this removes **31%** of departed
    names' bars and drops 48 of them outright for having none inside their own membership;
    on 4h it removes **75%** and drops 147, because a name that left the index in 2016
    cannot have a legitimate bar in a window that opens in 2019.

    Names that kept trading after leaving — `AA`, `GME`, `RIG` — lose their post-exit
    history too, and that is correct rather than collateral: this is a point-in-time S&P 500
    study, and those bars are bars of a company that was not in the index.
    """
    global _EXITS
    if _EXITS is None:
        path = DATA_DIR / "reference" / "sp500_membership.csv"
        if not path.exists():
            _EXITS = {}
        else:
            iv = pd.read_csv(path, parse_dates=["start", "end"])
            last = iv.groupby("symbol")["end"].max()
            # NaT is an open spell — a current member. Absent from the map entirely, so a
            # `.get` miss means "do not truncate" rather than "truncate at NaT".
            _EXITS = {str(s): e for s, e in last.items() if pd.notna(e)}
    return _EXITS


def describe_source() -> str:
    """Stamped into any artifact that outlives the session."""
    return "Twelve Data time_series, adjust=all"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(TIMEFRAMES))
    ap.add_argument("--symbols", nargs="+", default=None)
    args = ap.parse_args()

    for asset_class in args.classes:
        if CLASSES[asset_class].get("source", "twelvedata") != "twelvedata":
            print(f"\n=== {asset_class} === skipped: not a Twelve Data class. "
                  f"Run `python db_loader.py --class {asset_class}`.")
            continue
        for timeframe in args.timeframes:
            print(f"\n=== {asset_class} / {timeframe} ===")
            counts = fetch(asset_class, timeframe, args.symbols)
            if not counts:
                print("  nothing fetched")
                continue
            # The sample is whichever fetched symbol LOADS, not simply the first one.
            #
            # Fetching and loading do not answer the same question: `fetch` pulls the whole
            # union of names the class has ever held, while `load` returns only the bars
            # inside each name's membership span. On an intraday cell those can be disjoint
            # — `A` (Agilent) held a top-100 slot from 2001 to 2003 and the 4h cache starts
            # 2019, so it loads to nothing and `[first]` raised KeyError *after* all 88
            # windows had been fetched and written. A summary line must not be able to fail
            # a run whose data is already safely on disk.
            sample = None
            for cand in counts:
                got = load(asset_class, timeframe, [cand]).get(cand)
                if got is not None and len(got):
                    sample = got
                    break
            span = (f"{sample.index[0]} -> {sample.index[-1]}" if sample is not None
                    else "no symbol has bars inside its membership span at this timeframe")
            print(f"  {len(counts)} symbols | bars {min(counts.values()):,}-"
                  f"{max(counts.values()):,} | {span}")


if __name__ == "__main__":
    main()
