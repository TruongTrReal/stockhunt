"""Download and cache OHLCV data for the S&P 500 universe from Yahoo Finance.

Data is cached locally as one Parquet file per ticker under data/cache/, so
repeated backtests don't re-hit the network. Call `update_universe()` to
(re)download, and `load_universe()` to pull everything into memory.
"""

import time
from pathlib import Path

import pandas as pd
import yfinance as yf
from tqdm import tqdm

from sp500_tickers import get_sp500_tickers

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
DEFAULT_START = "2015-01-01"
BATCH_SIZE = 40
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}.parquet"


def _download_batch(tickers: list[str], start: str, end: str | None) -> dict[str, pd.DataFrame]:
    raw = yf.download(
        tickers=tickers,
        start=start,
        end=end,
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    out: dict[str, pd.DataFrame] = {}
    is_multi = isinstance(raw.columns, pd.MultiIndex)
    for ticker in tickers:
        try:
            df = raw[ticker].dropna(how="all") if is_multi else raw.dropna(how="all")
        except KeyError:
            continue
        if not df.empty:
            out[ticker] = df
    return out


def update_universe(
    tickers: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str | None = None,
    batch_size: int = BATCH_SIZE,
) -> list[str]:
    """Download fresh data for `tickers` (default: all S&P 500) and write to cache.

    Returns the list of tickers that failed to download after retries.
    """
    tickers = tickers or get_sp500_tickers()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    batches = [tickers[i : i + batch_size] for i in range(0, len(tickers), batch_size)]

    for batch in tqdm(batches, desc="Downloading S&P 500 batches"):
        pending = list(batch)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if not pending:
                break
            try:
                result = _download_batch(pending, start, end)
            except Exception as exc:  # network/yfinance hiccup, retry the whole batch
                if attempt == RETRY_ATTEMPTS:
                    failed.extend(pending)
                    print(f"Batch failed after {RETRY_ATTEMPTS} attempts: {exc}")
                    pending = []
                else:
                    time.sleep(RETRY_DELAY_SEC)
                continue

            for ticker, df in result.items():
                df.to_parquet(_cache_path(ticker))
            pending = [t for t in pending if t not in result]
            if pending and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)

        failed.extend(pending)

    if failed:
        print(f"{len(failed)} tickers failed to download: {failed}")
    return failed


def load_universe(tickers: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Load cached OHLCV data for `tickers` (default: everything in cache)."""
    if tickers is None:
        paths = sorted(CACHE_DIR.glob("*.parquet"))
    else:
        paths = [_cache_path(t) for t in tickers]

    data = {}
    for path in paths:
        if path.exists():
            data[path.stem] = pd.read_parquet(path)
    return data


if __name__ == "__main__":
    failed = update_universe()
    data = load_universe()
    print(f"Loaded {len(data)} tickers into cache at {CACHE_DIR}")
