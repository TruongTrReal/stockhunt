"""Download daily OHLCV from Twelve Data and cache it as Parquet.

Two things about this API matter enough to be worth stating up front:

**``adjust=all`` is mandatory here.** The default response — and ``adjust=splits`` —
is split-adjusted but *not* dividend-adjusted. AAPL on 2015-01-02 comes back as
27.3325 by default and 24.19803 with ``adjust=all``; the project's existing
yfinance cache (``auto_adjust=True``) has 24.192606. Backtesting a total-return
rule on split-only prices silently strips every dividend out of the result, so
the default would quietly change what the numbers mean.

**The API key never lands in this file.** It is read from ``TWELVEDATA_API_KEY``,
falling back to a gitignored ``.env.local`` next to this module.

Run directly to (re)fill the cache::

    ..\\.venv-bakeoff\\Scripts\\python.exe twelvedata_loader.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
TD_CACHE = HERE / "data_td"
ENV_FILE = HERE / ".env.local"

BASE_URL = "https://api.twelvedata.com/time_series"
START_DATE = "2015-01-01"
# Twelve Data caps a single time_series response at 5000 rows; ~2,900 daily bars
# since 2015 fits comfortably, but ask explicitly rather than take the default 30.
OUTPUT_SIZE = 5000
# Pro plan allows 610 credits/minute and one time_series call is 1 credit, so a
# 20-symbol pull is never near the limit. The pause is only here so that raising
# UNIVERSE to a few hundred names later cannot trip the limiter.
SLEEP_BETWEEN_CALLS = 0.15


def api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TWELVEDATA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "No Twelve Data API key. Set TWELVEDATA_API_KEY, or put "
        f"TWELVEDATA_API_KEY=... in {ENV_FILE} (gitignored)."
    )


def fetch(symbol: str, end_date: str | None = None) -> pd.DataFrame:
    """One symbol's dividend- and split-adjusted daily bars, oldest first."""
    params = {
        "symbol": symbol,
        "interval": "1day",
        "start_date": START_DATE,
        "outputsize": OUTPUT_SIZE,
        "adjust": "all",
        "order": "ASC",
        "apikey": api_key(),
    }
    if end_date:
        params["end_date"] = end_date

    response = requests.get(BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()

    # Twelve Data reports errors as HTTP 200 with status="error", so a bad symbol
    # or an exhausted quota looks like success unless this is checked.
    if payload.get("status") != "ok":
        raise RuntimeError(f"{symbol}: {payload.get('message', payload)}")
    values = payload.get("values")
    if not values:
        raise RuntimeError(f"{symbol}: no values returned")

    df = pd.DataFrame(values)
    df["Date"] = pd.to_datetime(df["datetime"])
    df = df.set_index("Date").sort_index()
    out = pd.DataFrame({
        "Open": pd.to_numeric(df["open"]),
        "High": pd.to_numeric(df["high"]),
        "Low": pd.to_numeric(df["low"]),
        "Close": pd.to_numeric(df["close"]),
        "Volume": pd.to_numeric(df["volume"]),
    })
    return out.dropna()


def update_cache(symbols: list[str], force: bool = False) -> dict[str, int]:
    """Fill ``data_td/`` with one Parquet per symbol. Returns bar counts."""
    TD_CACHE.mkdir(exist_ok=True)
    counts = {}
    for symbol in symbols:
        path = TD_CACHE / f"{symbol}.parquet"
        if path.exists() and not force:
            counts[symbol] = len(pd.read_parquet(path))
            continue
        df = fetch(symbol)
        df.to_parquet(path)
        counts[symbol] = len(df)
        print(f"  {symbol:6} {len(df):5} bars  "
              f"{df.index[0].date()} -> {df.index[-1].date()}")
        time.sleep(SLEEP_BETWEEN_CALLS)
    return counts


if __name__ == "__main__":
    from common import UNIVERSE

    print(f"Fetching {len(UNIVERSE)} symbols from Twelve Data (adjust=all)...")
    counts = update_cache(UNIVERSE, force=True)
    print(f"\ncached {len(counts)} symbols in {TD_CACHE}")
    print(f"bar counts: min={min(counts.values())} max={max(counts.values())}")
