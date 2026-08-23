"""Fetch CME futures OHLCV from Databento and turn it into a series you can backtest.

Twelve Data carries no futures at all -- every CME ticker there resolves to an equity
wearing the same letters -- so this class has its own vendor and its own loader. What it
writes is deliberately the *same* thing `td_loader` writes: a plain OHLCV parquet per
symbol under `../data/futures/<tf>/`, so `td_loader.load` stays the single door every
stage in the repo reaches its bars through, and nothing downstream learns there is a
second vendor.

Run::

    python db_loader.py --check                     # what it would cost, and nothing else
    python db_loader.py                             # the pool, daily
    python db_loader.py --symbols ES.v.0 CL.v.0
    python db_loader.py --budget 5.00               # allow up to $5 of metered data
    python db_loader.py --rebuild                   # re-derive from cached raw bars

Four things here are not cosmetic.

**A continuous futures series is a fiction, and an unadjusted one is a lie.** `CL.v.0`
is whichever WTI contract carried the most volume that day, so on a roll date the price
jumps to a different contract's level: in April 2020 the front month closed at 18.12 and
the series' next print was 24.76, a +37% "return" nobody earned. Left alone that gap is
attributed to whatever rule happened to be long. So the series is **ratio back-adjusted**
across every roll, which makes the return across a roll equal the return of the contract
you rolled into -- the return an actual roll would have earned.

**The roll ratio is measured between two contracts at the same instant, not across
time.** The usual splice takes the outgoing contract's last close against the incoming
contract's first close, which folds a session of market movement into the adjustment.
This loader fetches rank 1 (`ES.v.1`) alongside rank 0 and reads the incoming contract's
price on the *same bar* as the outgoing one, then verifies via `instrument_id` that rank
1 really was the contract rank 0 rolled into. When it was not -- ranks can skip -- it
falls back to the splice and says so in the roll ledger, so every adjustment in the
series is either exact or labelled.

**A vendor "day" is a UTC day, and the CME's is not.** `ohlcv-1d` buckets on the UTC
calendar, which cuts the session at 18:00 Chicago. Two consequences, one harmless and one
not. The harmless one: every bar runs from one session's open hour to the next one's, so
the close is a real 18:00 CT print rather than the 16:00 CT settlement -- tradable, since
Globex is open, but not the settlement price, and that convention is what "the close"
means on this class. The one that had to be fixed: **Sunday's 17:00 CT open falls in its
own UTC day and arrives as a separate two-hour bar** -- ES prints 5,600 contracts on a
Sunday against 1.3M on a Monday. Left alone that is 52 fake "days" a year, each a tiny
sliver whose range would feed straight into any volatility or reversion rule. Every
Sunday stub is merged into the session it opens (`merge_session_stubs`) -- except when the
weekend carried the roll, which 41% of them do, because then the sliver belongs to the
contract being left behind and merging it would build a bar whose Open is one instrument
and whose Close is another. Grains have no stub either way: they open at 19:00 CT, which
is already the next UTC day.

**Daily comes from `ohlcv-1d`, not from aggregating `ohlcv-1h`, and that was measured.**
Session-aligned bars cut from hourly data would be strictly better -- a true 16:00 CT
close and any intraday timeframe for free -- but the vendor's hourly archive is not
complete before 2013. On affected days the aggregation collapses a whole session into one
or two bars: 2015-01-06 returns 2 hourly bars whose volume sums to the full day's
2,344,424, and June 2011 returns 230 hourly bars where ~500 exist. `ohlcv-1d` over the
same days is complete and its volumes tie out to the hourly sum exactly, so the daily
schema is the trustworthy one and it is the one used.

    ohlcv-1h bars in June, ES     2010: 138   2011: 230   2012: 146
                                  2013: 465   2015: 509   2020: 505   2022: 502

That defect is why **this loader** ships 1d only, and the sentence above needs one
correction: "before 2013" is wrong in both directions. Re-measured 2026-08-22 across
every year on five consecutive weekdays, the folding is scattered by DAY, not bounded by
an era -- 2010-06-07 is complete (1,333 one-minute bars) while 2015-01-06 is not (114),
and `ohlcv-1m` carries the identical defect, not just `ohlcv-1h`:

    complete weekdays, ES, mid-June   2010-2012: 1 of 5     2013: 5/5   2014: 3/5
                                      2015: 5/5             2016-2026: 5 of 5, every year

The intraday timeframes therefore ARE reachable, and free -- see `db_intraday.py`, which
screens each session against that root's own median rather than trusting a start date.
The `trades`-schema rebuild costed below is only needed for the pre-2016 sample.

**What makes a folded day dangerous is that it reconciles.** Its volume sums to EXACTLY
the `ohlcv-1d` volume for the same session, so the missing minutes are not absent -- they
are folded into the bars that remain. Every check in this repo passes on one: the OHLC
relations hold, the volume ties out, there is no gap. Only the bar COUNT gives it away.
Same family as the foreign-namesake tickers and EEM's truncated history: well formed,
internally consistent, and quietly the wrong measurement.

**Nothing is downloaded before its price is known.** Every request is costed against
`metadata.get_cost` first and the run aborts if the total exceeds `--budget`, which
defaults to zero. On the current key CME OHLCV prices at $0.00 across all of history, but
a default that can silently spend is a default that eventually does.
"""

from __future__ import annotations

import argparse
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests
from tqdm import tqdm

from config import CLASS_DIR, CLASSES, DATA_DIR, ENV_FILE, cache_dir, safe_symbol
from futures_specs import GLBX_START, check_notional

BASE_URL = "https://hist.databento.com/v0/"
DATASET = "GLBX.MDP3"
SOURCE_SCHEMA = "ohlcv-1d"
# The one timeframe THIS module ships. Intraday is not unreachable -- `db_intraday.py`
# fetches 1h and 1m from 2016 with a per-session screen -- but it is a different path,
# because the two things this module does either side of the download (merging Sunday's
# stub into the session it opens, deriving the roll ratio from the second contract rank)
# are daily concepts that would be wrong applied to minute bars.
TIMEFRAME = "1d"
# The CSV endpoint stops at 5,000 rows. It is a hard cap -- passing `limit=1000000`
# changes nothing -- and it is announced inconsistently: a request that overruns it
# sometimes answers 206 and sometimes answers **200 with exactly 5,000 rows and no other
# sign at all**. The second form is the dangerous one, and it is why the row count is
# checked rather than the status code. Same reasoning as `td_loader.OUTPUT_SIZE`: a
# window that comes back at exactly the cap has almost certainly been truncated, and a
# truncated window is a silent hole in the middle of a series.
OUTPUT_SIZE = 5000
# Sized against that cap. A CME root prints ~310 daily bars a year once Sunday stubs are
# counted, so eight symbols over one year is ~2,500 rows -- half the cap, which leaves
# room for the roots that trade Sundays without making the bisection the common path.
CHUNK_YEARS = 2
BATCH_SIZE = 6
# Requests run concurrently because the cost of one is almost entirely latency: ~25
# seconds whether it carries 500 rows or 5,000, since the server spends it resolving
# continuous symbology. Serially the pool takes over an hour.
#
# FOUR, not more. At eight the vendor stops answering rather than answering slowly --
# seven requests complete and the next eight sit in flight indefinitely, which reads as a
# hung job rather than a throttled one. Four is comfortably inside whatever the limit is
# and still cuts the pull to about a quarter of the serial time.
WORKERS = 4
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5
# Short enough that a request the vendor has quietly stopped serving fails and is retried
# rather than holding a worker for ten minutes. The largest legitimate response here is a
# 5,000-row CSV, which arrives in well under a minute.
TIMEOUT_SEC = 180

ROLL_LEDGER = DATA_DIR / "reference" / "futures_rolls.csv"


# --------------------------------------------------------------------- vendor

def api_key() -> str:
    key = os.environ.get("DATABENTO_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABENTO_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"No Databento API key (env DATABENTO_API_KEY or {ENV_FILE})")


class PartialResponse(Exception):
    """The vendor answered 206: the stream stopped early and the body is incomplete.

    This is the failure mode that must not be papered over. A 206 body parses perfectly —
    it is well-formed CSV with a header and thousands of good rows — it just stops in the
    middle of 2013 and says nothing about it. Accepting one would put a multi-year hole in
    a series that every later stage would read as a shorter history rather than a broken
    one. The caller's job is to ask for less and ask again, never to keep what arrived.
    """

    def __init__(self, body: str):
        super().__init__("partial response (206)")
        self.body = body


def _call(path: str, method: str = "GET", **params):
    key = api_key()
    last = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            if method == "GET":
                r = requests.get(BASE_URL + path, params=params, auth=(key, ""),
                                 timeout=TIMEOUT_SEC)
            else:
                r = requests.post(BASE_URL + path, data=params, auth=(key, ""),
                                  timeout=TIMEOUT_SEC)
            if r.status_code == 206:
                # Not retried here: the same oversized request will stop in the same
                # place, and three attempts at it cost two minutes to learn nothing.
                raise PartialResponse(r.text)
            if r.status_code == 200:
                return r
            # 4xx are our fault and will not improve by asking again.
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:300]}")
            last = f"{r.status_code}: {r.text[:200]}"
        except requests.RequestException as exc:
            last = str(exc)
        if attempt == RETRY_ATTEMPTS:
            raise RuntimeError(f"{path} failed after {RETRY_ATTEMPTS} attempts: {last}")
        time.sleep(RETRY_DELAY_SEC * attempt)


_AVAILABLE_END: dict[str, str] = {}


def available_end(schema: str = SOURCE_SCHEMA) -> str:
    """The last date the vendor actually has, as a date string.

    Asking rather than assuming "today" is not defensive padding: Databento rejects the
    whole request with a 422 if `end` runs one hour past the archive, and the archive
    lags real time by a few hours. Without this the loader breaks every day at midnight
    UTC and works again by lunchtime, which is the worst kind of intermittent.
    """
    if schema not in _AVAILABLE_END:
        r = _call("metadata.get_dataset_range", dataset=DATASET).json()
        stamp = r.get("schema", {}).get(schema, r)["end"]
        _AVAILABLE_END[schema] = pd.Timestamp(stamp).strftime("%Y-%m-%d")
    return _AVAILABLE_END[schema]


# The vendor's way of saying "that contract did not exist yet". A continuous symbol only
# resolves over dates when the root was listed, so every chunk before SOFR (2018), Ultra
# 10-Year (2016), Bitcoin (2017) or Ether (2021) came to market answers with this 422.
# It is an empty window, not a failure, and treating it as one is what lets the pool hold
# short-history candidates for the screen to reject on evidence.
_UNLISTED = "None of the symbols could be resolved"


def cost_usd(symbols: list[str], start: str, end: str,
             schema: str = SOURCE_SCHEMA) -> float:
    """What the vendor says this exact request will be billed. Free to ask."""
    try:
        r = _call("metadata.get_cost", dataset=DATASET, symbols=",".join(symbols),
                  schema=schema, start=start, end=end, stype_in="continuous",
                  mode="historical-streaming")
    except RuntimeError as exc:
        if _UNLISTED in str(exc):
            return 0.0
        raise
    return float(r.json())


def _get_range(symbols: list[str], start: str, end: str,
               schema: str = SOURCE_SCHEMA) -> pd.DataFrame:
    try:
        r = _call("timeseries.get_range", method="POST", dataset=DATASET,
                  symbols=",".join(symbols), schema=schema, start=start, end=end,
                  stype_in="continuous", stype_out="instrument_id", encoding="csv",
                  compression="none", pretty_px="true", pretty_ts="true",
                  map_symbols="true")
    except RuntimeError as exc:
        if _UNLISTED in str(exc):
            return pd.DataFrame()
        raise
    if not r.text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(r.text))


# ------------------------------------------------------------------- fetching

def _chunks(start: str, end: str, years: int = CHUNK_YEARS) -> list[tuple[str, str]]:
    begin = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while begin < stop:
        nxt = min(begin.replace(year=begin.year + years), stop)
        out.append((begin.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        begin = nxt
    return out


def _frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Vendor CSV -> the project's OHLCV frame, plus the contract each bar belongs to."""
    if raw.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "Open": pd.to_numeric(raw["open"]),
        "High": pd.to_numeric(raw["high"]),
        "Low": pd.to_numeric(raw["low"]),
        "Close": pd.to_numeric(raw["close"]),
        # A futures volume is a contract count, but it is carried as float64 like every
        # other class so the volume-consuming TA-Lib rules see one dtype everywhere.
        "Volume": pd.to_numeric(raw["volume"], errors="coerce").astype("float64"),
        "instrument_id": pd.to_numeric(raw["instrument_id"]).astype("int64"),
    })
    idx = pd.to_datetime(raw["ts_event"], utc=True).dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx, name="Date")
    df = df.sort_index()
    return df[~df.index.duplicated(keep="first")].dropna(
        subset=["Open", "High", "Low", "Close"])


def _parse_partial(body: str) -> pd.DataFrame:
    """Read a 206 body, discarding a final line the stream stopped in the middle of."""
    lines = body.splitlines()
    if len(lines) < 2:
        return pd.DataFrame()
    header = lines[0]
    width = header.count(",")
    rows = [ln for ln in lines[1:] if ln.count(",") == width]
    if not rows:
        return pd.DataFrame()
    return pd.read_csv(io.StringIO("\n".join([header, *rows])))


def _fetch_window(symbols: list[str], a: str, b: str) -> pd.DataFrame:
    """One window of bars, resumed as many times as the vendor cuts the stream.

    Two different truncations have to be survived and they look nothing alike:

    * **200 with exactly `OUTPUT_SIZE` rows.** The row cap, announced by nothing at all.
    * **206 with a partial body.** The stream was cut mid-flight; the body is a valid
      prefix, sometimes ending in half a line.

    Both are answered the same way: keep what arrived, then ask again starting at the
    last timestamp delivered. Resuming rather than bisecting matters — bisection re-asks
    for a window that is *also* likely to be cut, so one interrupted stream becomes two
    requests, then four, and a job that is working hard is indistinguishable from a job
    that has hung. That is exactly what it looked like at eight workers.

    The cursor restarts *on* the last delivered date rather than after it, so a day that
    was split across two responses cannot lose its tail. The repeated bars are dropped by
    the per-symbol dedupe in `download`.
    """
    parts: list[pd.DataFrame] = []
    cursor = a
    while cursor < b:
        try:
            raw = _get_range(symbols, cursor, b)
            cut = len(raw) >= OUTPUT_SIZE
        except PartialResponse as exc:
            raw = _parse_partial(exc.body)
            cut = True
        if raw.empty:
            break
        parts.append(raw)
        if not cut:
            break
        last = pd.to_datetime(raw["ts_event"], utc=True).max().strftime("%Y-%m-%d")
        # Never stand still: if the whole response landed inside one day, step past it
        # rather than asking for the same window forever.
        nxt = last if last > cursor else (
            pd.Timestamp(cursor) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cursor = min(nxt, b)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def download(symbols: list[str], start: str, end: str,
             budget: dict) -> dict[str, pd.DataFrame]:
    """Raw daily bars per symbol. Rank 0 and rank 1 are fetched together.

    The whole pull is priced before any of it is downloaded, rather than job by job. It
    is the same total either way — resuming a cut stream re-requests the rows it already
    delivered but not the ones it did not — and a single up-front number is a decision
    the caller can actually make, where an incremental guard only tells you when to stop
    after you have already spent.
    """
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    jobs = [(b, w[0], w[1]) for b in batches for w in _chunks(start, end)]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        prices = list(tqdm(pool.map(lambda j: cost_usd(j[0], j[1], j[2]), jobs),
                           total=len(jobs), desc="costing"))
    budget["spent"] = float(sum(prices))
    if budget["spent"] > budget["limit"] + 1e-9:
        raise SystemExit(
            f"aborting before downloading anything: this pull is billed at "
            f"${budget['spent']:.2f}, over the ${budget['limit']:.2f} budget. "
            f"Raise it with --budget if that is intended.")

    collected: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_fetch_window, b, a, z): (b, a, z) for b, a, z in jobs}
        for fut in tqdm(as_completed(futures), total=len(futures),
                        desc=f"{DATASET} {SOURCE_SCHEMA} {start}..{end}"):
            raw = fut.result()
            if raw.empty:
                continue
            for symbol, part in raw.groupby("symbol"):
                if symbol in collected:
                    collected[symbol].append(_frame(part))
    out = {}
    for symbol, parts in collected.items():
        parts = [p for p in parts if not p.empty]
        if not parts:
            continue
        df = pd.concat(parts).sort_index()
        out[symbol] = df[~df.index.duplicated(keep="first")]
    return out


# ------------------------------------------------------------- session repair

def merge_session_stubs(df: pd.DataFrame) -> pd.DataFrame:
    """Fold Sunday's opening sliver into the session it opens.

    CME reopens at 17:00 Chicago on Sunday, which is still Sunday in UTC, so the vendor
    emits it as a bar of its own: two hours of trade standing next to five full sessions.
    On ES that is 5,600 contracts against 1.3M, and 52 of them a year — each one a bar
    with a real timestamp, a real range, and no business being scored as a day.

    Merging rather than dropping is the point. The sliver is genuinely the first two
    hours of Monday's session, so folding it in makes Monday's open the session's true
    open; dropping it would silently move Monday's open two hours later.

    **Unless the weekend contained the roll, and 41% of them do.** A continuous series
    rolls on volume, and volume rank flips over a weekend more often than on any weekday:
    2,305 of 5,585 rolls in this universe land on a Monday. When that happens the Sunday
    sliver belongs to the contract being left behind, so merging it would build a bar
    whose Open is one instrument and whose Close is another — displaced by the whole roll
    gap, a median 0.56% here. That is not a rounding error in a bar's geometry, it is a
    fabricated gap, and `IBS` is `(C-L)/(H-L)`: it reads exactly that geometry.

    So a stub whose contract is not the one the next session holds is **dropped**. The
    cost is one hour of the new contract's session open, since the UTC-day bar already
    starts at 18:00 CT; the alternative cost is a mixed-instrument bar on 1.3% of days.
    """
    if df.empty:
        return df
    if "instrument_id" in df.columns:
        is_sunday = pd.Series([d.weekday() == 6 for d in df.index], index=df.index)
        nxt = df["instrument_id"].shift(-1)
        stale_stub = is_sunday & nxt.notna() & (df["instrument_id"] != nxt)
        df = df[~stale_stub]
        if df.empty:
            return df
    # Sunday belongs to the next day; every other bar keeps its own date. Weekday 6 is
    # Sunday in pandas' Monday-zero convention.
    trade_date = pd.DatetimeIndex(
        [d + pd.Timedelta(days=1) if d.weekday() == 6 else d for d in df.index],
        name="Date")
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last",
           "Volume": "sum", "instrument_id": "last"}
    out = df.groupby(trade_date, sort=True).agg(
        {k: v for k, v in agg.items() if k in df.columns})
    return out


# ---------------------------------------------------------------- adjustment

def back_adjust(front: pd.DataFrame, behind: pd.DataFrame,
                symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Ratio back-adjust `front` across every roll. Returns (bars, roll ledger).

    The series is anchored at the *newest* contract, so the last bars are untouched real
    prices and history is scaled to them. That is the orientation the paper desk needs --
    a live signal computed on this series is computed on today's actual quote -- and it
    is why the adjustment runs backwards rather than forwards from 2010.
    """
    if front.empty:
        return front, pd.DataFrame()

    ids = front["instrument_id"].to_numpy()
    positions = [i for i in range(1, len(ids)) if ids[i] != ids[i - 1]]

    behind_ids = behind_close = None
    if not behind.empty:
        behind_ids = behind["instrument_id"].reindex(front.index).to_numpy()
        behind_close = behind["Close"].reindex(front.index).to_numpy()

    rows, ratios = [], {}
    for i in positions:
        prev_close = float(front["Close"].iloc[i - 1])
        new_close = float(front["Close"].iloc[i])
        exact = False
        # The incoming contract's price on the SAME bar as the outgoing one -- but only
        # if rank 1 on that bar really was the contract rank 0 became. Ranks can skip a
        # month, and using rank 1 blindly would price the roll against a contract that
        # was never traded into.
        if (behind_ids is not None and not pd.isna(behind_ids[i - 1])
                and int(behind_ids[i - 1]) == int(ids[i])
                and not pd.isna(behind_close[i - 1])
                and float(behind_close[i - 1]) > 0 and prev_close > 0):
            ratio = float(behind_close[i - 1]) / prev_close
            exact = True
        else:
            # Fallback: the ordinary splice. It folds one bar of market movement into
            # the adjustment, so it is recorded as inexact rather than hidden.
            ratio = new_close / prev_close if prev_close > 0 else 1.0
        ratios[i] = ratio
        rows.append({
            "symbol": symbol,
            "roll_date": front.index[i].date().isoformat(),
            "from_instrument_id": int(ids[i - 1]),
            "to_instrument_id": int(ids[i]),
            "old_close": prev_close,
            "new_close": new_close,
            "ratio": ratio,
            "gap_pct": 100.0 * (ratio - 1.0),
            "method": "same-bar rank 1" if exact else "close-to-close splice",
        })

    # Cumulative factor at bar j = product of every roll ratio strictly after j.
    running = 1.0
    values = [1.0] * len(front)
    for j in range(len(front) - 1, -1, -1):
        values[j] = running
        if j in ratios:
            running *= ratios[j]
    factor = pd.Series(values, index=front.index)

    out = front.copy()
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col] * factor

    if (out[["Open", "High", "Low", "Close"]] <= 0).to_numpy().any():
        raise ValueError(
            f"{symbol}: ratio back-adjustment produced a non-positive price. That means "
            f"the front contract printed at or through zero (WTI did, in April 2020), "
            f"and a multiplicative adjustment cannot represent it. This series needs "
            f"difference adjustment or exclusion -- it must not be swept as is.")

    return out.drop(columns=["instrument_id"]), pd.DataFrame(rows)


# --------------------------------------------------------------------- driver

def _rank_one(symbol: str) -> str:
    root, rule, _ = symbol.split(".")
    return f"{root}.{rule}.1"


def _raw_dir(asset_class: str):
    return DATA_DIR / CLASS_DIR[asset_class] / "_raw"


def save_raw(asset_class: str, raw: dict[str, pd.DataFrame]) -> None:
    """Keep the unadjusted vendor bars, contract ids and all.

    Everything downstream of the download is a *transform* — merging the session stubs,
    pricing the rolls, back-adjusting — and transforms get fixed. Without this the only
    way to re-derive the series after a fix is to re-download it, which is forty minutes
    against the vendor for data that has not changed. The raw bars are the input; keeping
    them makes `--rebuild` possible and makes any future correction cheap.
    """
    out = _raw_dir(asset_class)
    out.mkdir(parents=True, exist_ok=True)
    for symbol, df in raw.items():
        df.to_parquet(out / f"{safe_symbol(symbol)}.parquet")


def load_raw(asset_class: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol in symbols:
        path = _raw_dir(asset_class) / f"{safe_symbol(symbol)}.parquet"
        if path.exists():
            out[symbol] = pd.read_parquet(path)
    return out


def fetch(asset_class: str = "cme_futures", symbols: list[str] | None = None,
          start: str = GLBX_START, end: str | None = None,
          budget_usd: float = 0.0, rebuild: bool = False) -> dict[str, int]:
    """Download, repair the session, adjust the rolls and cache. Bars written per symbol.

    `rebuild=True` re-derives everything from the cached raw bars and asks the vendor for
    nothing.
    """
    wanted = list(symbols) if symbols else list(CLASSES[asset_class]["symbols"])
    both = wanted + [_rank_one(s) for s in wanted]

    if rebuild:
        raw = load_raw(asset_class, both)
        missing = [s for s in wanted if s not in raw]
        if missing:
            raise SystemExit(
                f"--rebuild has no raw bars for {', '.join(missing[:5])}"
                f"{'...' if len(missing) > 5 else ''}. Run without it once first.")
    else:
        end = min(end or available_end(), available_end())
        budget = {"spent": 0.0, "limit": float(budget_usd)}
        raw = download(both, start, end, budget)
        save_raw(asset_class, raw)

    out_dir = cache_dir(asset_class, TIMEFRAME)
    out_dir.mkdir(parents=True, exist_ok=True)

    counts, ledgers = {}, []
    for symbol in wanted:
        front = raw.get(symbol)
        if front is None or front.empty:
            print(f"  {symbol}: no data")
            continue
        behind = raw.get(_rank_one(symbol), pd.DataFrame())
        front = merge_session_stubs(front)
        if not behind.empty:
            behind = merge_session_stubs(behind)
        try:
            bars, ledger = back_adjust(front, behind, symbol)
        except ValueError as exc:
            print(f"  {symbol}: {exc}")
            continue
        # The last bar is an unadjusted, real quote -- the one place the contract table's
        # `price_scale` can be checked against a price a human can recognise.
        check_notional(symbol.split(".")[0], float(front["Close"].iloc[-1]))
        if not ledger.empty:
            ledgers.append(ledger)
        bars.to_parquet(out_dir / f"{safe_symbol(symbol)}.parquet")
        counts[symbol] = len(bars)

    if ledgers:
        ROLL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        book = pd.concat(ledgers, ignore_index=True)
        if ROLL_LEDGER.exists():
            old = pd.read_csv(ROLL_LEDGER)
            old = old[~old["symbol"].isin(book["symbol"].unique())]
            book = pd.concat([old, book], ignore_index=True)
        book.sort_values(["symbol", "roll_date"]).to_csv(ROLL_LEDGER, index=False)
    return counts


def describe_source() -> str:
    return (f"Databento {DATASET} continuous ({SOURCE_SCHEMA}, volume roll), "
            f"Sunday stubs merged, ratio back-adjusted")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--class", dest="asset_class", default="cme_futures")
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--start", default=GLBX_START)
    ap.add_argument("--end")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="most this run may be billed, in USD. Default 0: free only.")
    ap.add_argument("--check", action="store_true",
                    help="price the whole pull and exit without downloading anything")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-derive from the cached raw bars; asks the vendor for nothing")
    args = ap.parse_args()

    symbols = args.symbols or list(CLASSES[args.asset_class]["symbols"])
    end = None if args.rebuild else min(args.end or available_end(), available_end())
    both = symbols + [_rank_one(s) for s in symbols]

    if args.check:
        batches = [both[i:i + BATCH_SIZE] for i in range(0, len(both), BATCH_SIZE)]
        jobs = [(b, w[0], w[1]) for b in batches for w in _chunks(args.start, end)]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            total = sum(tqdm(pool.map(lambda j: cost_usd(j[0], j[1], j[2]), jobs),
                             total=len(jobs), desc="costing"))
        print(f"\n{len(symbols)} symbols x 2 ranks, {SOURCE_SCHEMA}, "
              f"{args.start}..{end}: ${total:.4f}")
        return

    counts = fetch(args.asset_class, symbols, start=args.start, end=end,
                   budget_usd=args.budget, rebuild=args.rebuild)
    print(f"\n{len(counts)}/{len(symbols)} symbols cached. {describe_source()}")
    for symbol, n in sorted(counts.items()):
        print(f"  {symbol:10s} {n:>7,} daily bars")


if __name__ == "__main__":
    main()
