"""Fetch the deepest daily history yfinance serves for the top-20 book, back to 1995.

Motivation is arithmetic, not hope. Significance of an edge is t = IR * sqrt(years), so
the 8-year sample caps every t-statistic at IR * 2.83 — an IR of 0.5, a genuinely good
system, scores 1.41 and cannot be proven. Reaching back to ~2000 takes sqrt(years) from
2.83 to ~5.1 and nearly doubles every t-stat computed from here on. It also adds the
dot-com bust and the GFC, two stress regimes that neither the current train window
(2015-2022) nor the holdout (2023-2026) contains at all.

Written to `data/cache_1d_long/`, a SEPARATE directory from the Twelve Data cache.
The two vendors are not interchangeable — twelvedata-vs-yfinance measured equity gaps up
to 18% on near-identical signals — so nothing here may be concatenated with cache_td.
Every long-history result is a yfinance-only result and gets labelled that way.

TWO BIASES THAT DO NOT CANCEL, and must be stated wherever these numbers are used:

  1. The universe is today's 20 largest US companies, selected with 2026 hindsight. In
     2000 NVDA was a small-cap and AMZN was a dot-com survivor-to-be. Holding this
     basket since 1995 is enormous look-ahead — ABSOLUTE returns from this cache are
     meaningless and must never be quoted as "what buy-and-hold earned".
  2. Names enter as they list (META 2012, ABBV 2013, V 2008, MA 2006, TSLA 2010), so the
     early panel is unbalanced. The book equal-weights whatever exists on each date,
     which is what an investor could actually have done.

What DOES survive is the relative comparison. survivorship-bias-measured already found
the bias inflates buy-and-hold by 4.85pp of CAGR while leaving the strategy's edge over
buy-and-hold unchanged. IR is measured against buy-and-hold on the same names over the
same dates, so the shared inflation cancels out of the difference series. That is the
only thing this cache is licensed to measure.

    python edge/fetch_long_history.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "data" / "cache_1d_long"
START = "1995-01-01"
UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]
BENCH = "SPY"


def main() -> None:
    import yfinance as yf

    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    for ticker in UNIVERSE + [BENCH]:
        for attempt in (1, 2, 3):
            try:
                df = yf.download(ticker, start=START, auto_adjust=True, progress=False,
                                 actions=False)
                break
            except Exception as exc:
                if attempt == 3:
                    print(f"  {ticker}: FAILED {exc}")
                    df = None
                else:
                    time.sleep(3)
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        df.to_parquet(CACHE / f"{ticker}.parquet")
        rows.append({"ticker": ticker, "bars": len(df),
                     "start": df.index[0].date(), "end": df.index[-1].date()})
        print(f"  {ticker:>6}: {len(df):>6,} bars  {df.index[0].date()} -> {df.index[-1].date()}")

    r = pd.DataFrame(rows)
    print(f"\n{len(r)} tickers cached to {CACHE}")
    print(f"earliest {r['start'].min()}   latest start {r['start'].max()}")
    counts = pd.Series({str(y): int((pd.to_datetime(r['start']).dt.year <= y).sum())
                        for y in (1995, 2000, 2005, 2010, 2013, 2015)})
    print("\nnames available by year (why the early panel is thin):")
    print(counts.to_string())


if __name__ == "__main__":
    main()
