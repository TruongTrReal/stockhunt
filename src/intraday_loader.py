"""Download and cache intraday OHLCV bars from Yahoo Finance.

Yahoo's free intraday history is hard-capped: 5-minute bars only go back
~60 days, 1-hour bars only ~730 days. That's a much smaller sample than the
10+ years used for the daily backtest, so intraday results are noisier and
cover far fewer distinct market regimes - see the report's methodology notes.
"""

import time
from pathlib import Path

import pandas as pd
import yfinance as yf
from tqdm import tqdm

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache_intraday"
INTERVAL_PERIODS = {"5m": "60d", "1h": "730d"}
BATCH_SIZE = 20
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5


def _cache_path(ticker: str, interval: str) -> Path:
    return CACHE_DIR / f"{ticker}_{interval}.parquet"


def _download_batch(tickers: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    raw = yf.download(
        tickers=tickers, period=period, interval=interval,
        group_by="ticker", auto_adjust=True, threads=True, progress=False,
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


def update_intraday_universe(tickers: list[str], interval: str, batch_size: int = BATCH_SIZE) -> list[str]:
    """Download fresh intraday bars for `tickers` at `interval` ('5m' or '1h')."""
    period = INTERVAL_PERIODS[interval]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    batches = [tickers[i : i + batch_size] for i in range(0, len(tickers), batch_size)]

    for batch in tqdm(batches, desc=f"Downloading {interval} bars"):
        pending = list(batch)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if not pending:
                break
            try:
                result = _download_batch(pending, interval, period)
            except Exception as exc:
                if attempt == RETRY_ATTEMPTS:
                    failed.extend(pending)
                    print(f"Batch failed after {RETRY_ATTEMPTS} attempts: {exc}")
                    pending = []
                else:
                    time.sleep(RETRY_DELAY_SEC)
                continue

            for ticker, df in result.items():
                df.to_parquet(_cache_path(ticker, interval))
            pending = [t for t in pending if t not in result]
            if pending and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)

        failed.extend(pending)

    if failed:
        print(f"{len(failed)} tickers failed to download at {interval}: {failed}")
    return failed


def load_intraday_universe(tickers: list[str] | None, interval: str) -> dict[str, pd.DataFrame]:
    if tickers is None:
        paths = sorted(CACHE_DIR.glob(f"*_{interval}.parquet"))
    else:
        paths = [_cache_path(t, interval) for t in tickers]

    data = {}
    for path in paths:
        if path.exists():
            ticker = path.stem.removesuffix(f"_{interval}")
            data[ticker] = pd.read_parquet(path)
    return data
