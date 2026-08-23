"""CME intraday bars from Databento, screened for the archive's silent gaps.

A SEPARATE PATH FROM `db_loader.fetch`, deliberately. That one builds the daily class and
does two things which are daily concepts and would be wrong here: it merges Sunday's
two-hour stub into the session it opens, and it derives the roll ratio by fetching the
second contract rank alongside the first. Minute bars need neither -- a Sunday stub is
just the first 120 minutes of the week, and the ratios are already recorded.

**The archive is not uniformly complete, and the incomplete days LIE.** Measured
2026-08-22 on `ES.v.0`, five consecutive weekdays in mid-June of each year:

    2010  1/5 complete      2013  5/5           2016..2026  5/5 every year
    2011  1/5               2014  3/5
    2012  1/5               2015  5/5

A broken day returns ~110-250 bars where ~1,380 exist -- and its volume sums to EXACTLY
the `ohlcv-1d` volume for the same session. So the missing minutes are not absent, they
are folded into the bars that remain: coarse bars wearing a one-minute label. No volume
reconciliation, no OHLC-integrity check and no gap test can see it, which is the same
shape of defect as the foreign-namesake tickers and the truncated EEM history -- data
that is well formed and quietly the wrong measurement.

`db_loader`'s docstring dates this defect "before 2013" for the hourly schema. That is
wrong in both directions: 2010-06-07 is complete and 2015-01-06 is not (114 bars against
a 2,344,424-contract session). It is scattered by day, not an era boundary, so the answer
is a per-session screen rather than a start date -- `INTRADAY_START` is only where the
screen stops throwing most of the sample away.

Run::

    python db_intraday.py --tf 1h                    # every screened root
    python db_intraday.py --tf 1m --symbols ES.v.0   # one, to look at it
    python db_intraday.py --tf 1m --check            # price it and count sessions, no write
"""

from __future__ import annotations

import argparse
import io
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import db_loader
from config import CLASSES, DATA_DIR, cache_dir, safe_symbol

# Databento serves ohlcv at these four resolutions and no others -- there is no 15m
# schema, which is the whole reason 1m has to be fetched: 15m and 4h are built from it.
SCHEMA = {"1m": "ohlcv-1m", "1h": "ohlcv-1h"}

# The first year the measured scan found every sampled weekday complete. Earlier bars are
# still screened rather than trusted; this only stops the screen discarding four years of
# mostly-broken sessions one request at a time.
INTRADAY_START = "2016-01-01"

# **The screen is relative to each root's OWN normal session, never an absolute count.**
# Session length is a property of the contract, not of the exchange: measured 2024, one
# UTC day is 23 hourly bars on ES, 19 on the grains (ZS/ZC/ZL/ZW/ZM) and **6** on live
# cattle, which keeps pit hours. An absolute floor tuned to the equity index deleted
# LE.v.0 in its entirety -- 16,083 real bars, "every session screened out" -- while
# reading as a successful run for the other fifteen roots. So each symbol's median day is
# measured first and a session is folded only if it falls below this fraction of it.
#
# A median is safe here because the sample starts in 2016, where the measured completeness
# scan found every sampled weekday intact; on a mostly-folded era the median would itself
# be folded and the screen would pass everything. That is the other reason for
# INTRADAY_START -- not just yield, but a trustworthy reference session.
SESSION_FRAC = 0.5
# Below this a "day" is a stub or a holiday remnant rather than a session, whatever the
# root's median says.
ABS_FLOOR = {"1m": 60, "1h": 3}

# The CSV endpoint stops at 5,000 rows and sometimes announces it as a 200. Ask for less
# than that per request and the ambiguity never arises: ~3 sessions at 1m, ~200 at 1h.
CHUNK_DAYS = {"1m": 3, "1h": 180}

# The vendor answers a burst by closing the connection rather than by slowing down.
WORKERS = 4
ROLL_LEDGER = DATA_DIR / "reference" / "futures_rolls.csv"


def _get(symbol: str, schema: str, start: str, end: str) -> pd.DataFrame:
    """One chunk of bars. A 206 is refused, never kept: a partial body parses perfectly."""
    for attempt in range(4):
        try:
            r = db_loader._call(
                "timeseries.get_range", method="POST", dataset=db_loader.DATASET,
                symbols=symbol, schema=schema, start=start, end=end,
                stype_in="continuous", stype_out="instrument_id", encoding="csv",
                compression="none", pretty_px="true", pretty_ts="true",
                map_symbols="true")
            if not r.text.strip():
                return pd.DataFrame()
            return pd.read_csv(io.StringIO(r.text))
        except db_loader.PartialResponse:
            # Asking for a smaller window is the fix; the caller's chunk is already
            # sized under the cap, so this means the vendor cut it for another reason.
            time.sleep(2 * (attempt + 1))
        except RuntimeError as exc:
            if db_loader._UNLISTED in str(exc):
                return pd.DataFrame()
            time.sleep(3 * (attempt + 1))
        except Exception:                              # noqa: BLE001 - retried below
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{symbol} {schema} {start}..{end}: unreachable after 4 attempts")


def download(symbol: str, tf: str, start: str, end: str) -> pd.DataFrame:
    """Every bar for one continuous symbol, chunked under the row cap."""
    schema, step = SCHEMA[tf], CHUNK_DAYS[tf]
    out, cur = [], pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while cur < stop:
        nxt = min(cur + pd.Timedelta(days=step), stop)
        df = _get(symbol, schema, cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"))
        if not df.empty:
            out.append(df)
        cur = nxt
    if not out:
        return pd.DataFrame()
    bars = pd.concat(out, ignore_index=True)
    bars["ts_event"] = pd.to_datetime(bars["ts_event"], utc=True, format="mixed")
    return bars.drop_duplicates("ts_event").sort_values("ts_event")


def screen_sessions(bars: pd.DataFrame, tf: str) -> tuple[pd.DataFrame, dict]:
    """Drop the folded sessions. Returns the kept bars and what was thrown away.

    The screen is on BAR COUNT PER SESSION DAY, which is the only signal that separates a
    folded day from a complete one -- volume ties out either way, and that is precisely
    what makes the defect invisible to every other check in this repo.
    """
    if bars.empty:
        return bars, {"kept": 0, "dropped": 0, "dropped_days": []}
    # **UTC day, not Chicago day**, and the difference is not cosmetic. The daily class
    # groups by UTC calendar day -- verified 2026-08-22: aggregating these bars that way
    # reproduces `data/futures/1d` to 0.00 bp on open, high, low AND close with the volume
    # ratio exactly 1.0000, while Chicago grouping misses close by 13 bp. Screening on a
    # different boundary from the one the data aggregates on splits one session across two
    # buckets, so a complete day can read short in both: it threw away 16.8% of ZS.v.0 --
    # 649 sessions of soybeans whose 19 UTC bars fell either side of Chicago midnight.
    day = bars["ts_event"].dt.date
    counts = day.value_counts()
    # Sunday's evening reopen is a legitimately short session, not a folded one: it is the
    # first minutes of the trading week and every bar of it is real.
    sunday = pd.Series(list(counts.index)).map(
        lambda d: pd.Timestamp(d).weekday() == 6).values
    normal = float(counts[~sunday].median()) if (~sunday).any() else float(counts.median())
    floor = max(SESSION_FRAC * normal, ABS_FLOOR[tf])
    bad = set(counts.index[(counts < floor) & ~sunday])
    keep = ~day.isin(bad)
    return bars[keep].copy(), {"kept": int(keep.sum()), "dropped": int((~keep).sum()),
                               "normal_session": int(normal), "floor": int(floor),
                               "dropped_days": sorted(str(d) for d in bad)}


def apply_rolls(bars: pd.DataFrame, symbol: str, ledger: pd.DataFrame) -> pd.DataFrame:
    """Ratio back-adjust minute bars using the ledger the DAILY run already wrote.

    The daily stage computes each roll's ratio from the two contracts' closes on the roll
    date and records it in `futures_rolls.csv`. Re-deriving it here would mean fetching
    the second rank at minute resolution -- twice the requests to recompute a number that
    is already on disk, and a second definition of the same adjustment that could drift
    from the daily series. Every bar strictly before a roll is multiplied by that roll's
    ratio, cumulatively, which is what makes the newest bars the only real quotes.
    """
    rolls = ledger[ledger["symbol"] == symbol]
    if rolls.empty or bars.empty:
        return bars
    rolls = rolls.sort_values("roll_date")
    dates = pd.to_datetime(rolls["roll_date"], utc=True)
    ratios = rolls["ratio"].astype(float).values
    # Cumulative product from the END backwards: a bar before roll k is adjusted by the
    # product of every ratio from k onwards.
    factor = pd.Series(1.0, index=bars.index)
    cum = 1.0
    for d, r in zip(reversed(list(dates)), reversed(list(ratios))):
        cum *= r
        factor[bars["ts_event"] < d] = cum
    out = bars.copy()
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = out[col].astype(float) * factor
    return out


def to_ohlcv(bars: pd.DataFrame) -> pd.DataFrame:
    """The repo's cache shape: DatetimeIndex + Open/High/Low/Close/Volume.

    **The index is tz-NAIVE UTC**, which is the whole cache's convention -- every daily
    futures parquet and every Twelve Data series is stored that way, and `td_loader.load`
    compares the index against naive timestamps. Leaving the UTC tzinfo attached is not a
    cosmetic difference: it raises `TypeError: Invalid comparison between
    dtype=datetime64[ns, UTC] and Timestamp` the first time the pipeline tries to span-cut
    the series, so the data reads fine in pandas and is unusable by every stage above it.
    The wall clock stays UTC, which is what the daily bars are grouped on.
    """
    out = bars.rename(columns={"open": "Open", "high": "High", "low": "Low",
                               "close": "Close", "volume": "Volume"})
    out = out.set_index("ts_event")[["Open", "High", "Low", "Close", "Volume"]]
    out.index = pd.DatetimeIndex(out.index).tz_convert("UTC").tz_localize(None)
    out.index.name = "datetime"
    return out


def run(tf: str, symbols: list[str] | None, start: str, end: str | None,
        check: bool) -> None:
    syms = symbols or list(CLASSES["cme_futures"]["symbols"])
    end = end or db_loader.available_end(SCHEMA[tf])
    ledger = pd.read_csv(ROLL_LEDGER) if ROLL_LEDGER.exists() else pd.DataFrame(
        columns=["symbol", "roll_date", "ratio"])
    out_dir = cache_dir("cme_futures", tf)
    out_dir.mkdir(parents=True, exist_ok=True)

    cost = db_loader.cost_usd(syms, start, end, SCHEMA[tf])
    print(f"{len(syms)} roots, {SCHEMA[tf]}, {start}..{end}   vendor cost ${cost:,.2f}")
    if check:
        print("--check: nothing fetched, nothing written")
        return

    def one(sym: str) -> str:
        raw = download(sym, tf, start, end)
        if raw.empty:
            return f"  {sym:10s} no data"
        kept, info = screen_sessions(raw, tf)
        if kept.empty:
            return f"  {sym:10s} every session screened out ({info['dropped']} bars)"
        adj = apply_rolls(kept, sym, ledger)
        to_ohlcv(adj).to_parquet(out_dir / f"{safe_symbol(sym)}.parquet")
        pct = 100.0 * info["dropped"] / max(info["kept"] + info["dropped"], 1)
        return (f"  {sym:10s} {info['kept']:>9,} kept, {info['dropped']:>6,} screened "
                f"({pct:4.1f}%), {len(info['dropped_days']):>3} folded · normal session "
                f"{info['normal_session']} bars, floor {info['floor']}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for line in pool.map(one, syms):
            print(line, flush=True)
    print(f"\nwrote {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tf", choices=sorted(SCHEMA), required=True)
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--start", default=INTRADAY_START)
    ap.add_argument("--end")
    ap.add_argument("--check", action="store_true",
                    help="price it and stop; write nothing")
    a = ap.parse_args()
    run(a.tf, a.symbols, a.start, a.end, a.check)


if __name__ == "__main__":
    main()
