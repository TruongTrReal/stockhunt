"""Fetch daily + intraday bars for the top-20 universe from Twelve Data.

Twelve Data caps a `time_series` response at 5000 bars **per symbol**, so anything
below daily has to be paginated: the window sizes in `config.TIMEFRAMES` are chosen
so one request per window stays under that cap (5-minute bars run ~78/day, so an
80-calendar-day window is ~4,200 bars). Symbols are batched because the cost is
1 credit per symbol either way, but the wall-clock is per *request*.

Two API quirks are handled here and both were found the hard way:
  * a batch is capped at 20 symbols and reports the overflow as `414 URI Too Long`;
  * share classes are dot-separated (`BRK.B`), not dash-separated.

Run directly::

    python td_loader.py            # all three timeframes
    python td_loader.py 5m         # just one
"""

from __future__ import annotations

import sys
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd
import requests
from tqdm import tqdm

from config import DATA_DIR, TIMEFRAMES, UNIVERSE, cache_dir

BASE_URL = "https://api.twelvedata.com/time_series"
ENV_FILE = DATA_DIR.parent.parent / ".env.local"
MAX_SYMBOLS_PER_REQUEST = 20
# Smaller than the cap for intraday: 20 symbols x 5000 bars is a ~15 MB JSON body,
# and a slow parse of one oversized response costs more than two clean requests.
BATCH_SIZE = 5
OUTPUT_SIZE = 5000
CREDITS_PER_MINUTE = 550
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

_credit_log: deque[float] = deque()


def api_key() -> str:
    import os
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TWELVEDATA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"No Twelve Data API key (env TWELVEDATA_API_KEY or {ENV_FILE})")


def _spend_credits(n: int) -> None:
    while True:
        now = time.monotonic()
        while _credit_log and now - _credit_log[0] > 60.0:
            _credit_log.popleft()
        if len(_credit_log) + n <= CREDITS_PER_MINUTE:
            break
        time.sleep(max(0.1, 60.0 - (now - _credit_log[0])))
    stamp = time.monotonic()
    _credit_log.extend([stamp] * n)


def _api_symbol(ticker: str) -> str:
    """`BRK-B` -> `BRK.B`; Twelve Data 404s on the dash form."""
    return ticker.replace("-", ".")


def _to_frame(values: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(values)
    df["Date"] = pd.to_datetime(df["datetime"])
    df = df.set_index("Date").sort_index()
    out = pd.DataFrame({
        "Open": pd.to_numeric(df["open"]),
        "High": pd.to_numeric(df["high"]),
        "Low": pd.to_numeric(df["low"]),
        "Close": pd.to_numeric(df["close"]),
        "Volume": pd.to_numeric(df["volume"]).fillna(0).astype("int64"),
    })
    return out.dropna()


def _request(tickers: list[str], interval: str, start: str,
             end: str) -> dict[str, pd.DataFrame]:
    if len(tickers) > MAX_SYMBOLS_PER_REQUEST:
        mid = len(tickers) // 2
        out = _request(tickers[:mid], interval, start, end)
        out.update(_request(tickers[mid:], interval, start, end))
        return out

    api_names = {_api_symbol(t): t for t in tickers}
    params = {
        "symbol": ",".join(api_names), "interval": interval,
        "start_date": start, "end_date": end, "outputsize": OUTPUT_SIZE,
        "adjust": "all", "order": "ASC", "apikey": api_key(),
    }
    _spend_credits(len(tickers))
    response = requests.get(BASE_URL, params=params, timeout=180)
    if response.status_code == 414 and len(tickers) > 1:
        mid = len(tickers) // 2
        out = _request(tickers[:mid], interval, start, end)
        out.update(_request(tickers[mid:], interval, start, end))
        return out
    response.raise_for_status()
    payload = response.json()

    if len(api_names) == 1:
        payload = {next(iter(api_names)): payload}

    out: dict[str, pd.DataFrame] = {}
    for api_name, ticker in api_names.items():
        entry = payload.get(api_name)
        # "No data on the specified dates" is an expected, non-fatal answer when a
        # pagination window lands before a symbol's listing or over a holiday run.
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        values = entry.get("values")
        if values:
            frame = _to_frame(values)
            if not frame.empty:
                out[ticker] = frame
    return out


def _windows(start: str, window_days: int) -> list[tuple[str, str]]:
    """Consecutive [start, end) date windows from `start` to today."""
    begin = datetime.strptime(start, "%Y-%m-%d")
    today = datetime.utcnow() + timedelta(days=1)
    out = []
    while begin < today:
        end = min(begin + timedelta(days=window_days), today)
        out.append((begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        begin = end
    return out


def fetch_timeframe(timeframe: str, symbols: list[str] | None = None,
                    start: str | None = None) -> dict[str, int]:
    """Fetch one timeframe into its cache directory.

    `start` overrides the timeframe's default. The ETFs need it: they list in 2010 while
    the mega-cap window opens in 2015, and fetching them on the shared start would throw
    away five years of the only history that lowers the noise ceiling.
    """
    spec = TIMEFRAMES[timeframe]
    out_dir = cache_dir(timeframe)
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = symbols or UNIVERSE
    windows = _windows(start or spec["start"], spec["window_days"])
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    collected: dict[str, list[pd.DataFrame]] = {t: [] for t in symbols}

    jobs = [(b, w) for b in batches for w in windows]
    for batch, (start, end) in tqdm(jobs, desc=f"{timeframe} ({len(windows)} windows)"):
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                for ticker, frame in _request(batch, spec["interval"], start, end).items():
                    collected[ticker].append(frame)
                break
            except Exception as exc:
                if attempt == RETRY_ATTEMPTS:
                    print(f"  window {start}..{end} failed for {batch}: {exc}")
                else:
                    time.sleep(RETRY_DELAY_SEC)

    counts = {}
    for ticker, frames in collected.items():
        if not frames:
            print(f"  {ticker}: no data")
            continue

        # Windows are half-open but the API is inclusive at both ends, so adjacent
        # windows can repeat a boundary bar — drop duplicate timestamps.
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        df.to_parquet(out_dir / f"{ticker}.parquet")
        counts[ticker] = len(df)
    return counts


def load(timeframe: str, tickers: list[str] | None = None) -> dict[str, pd.DataFrame]:
    out_dir = cache_dir(timeframe)
    wanted = tickers or UNIVERSE
    data = {}
    for ticker in wanted:
        path = out_dir / f"{ticker}.parquet"
        if path.exists():
            data[ticker] = pd.read_parquet(path)
    return data


if __name__ == "__main__":
    requested = sys.argv[1:] or list(TIMEFRAMES)
    for timeframe in requested:
        print(f"\n=== {timeframe} ===")
        counts = fetch_timeframe(timeframe)
        if counts:
            sample = load(timeframe, [next(iter(counts))])[next(iter(counts))]
            print(f"  {len(counts)}/{len(UNIVERSE)} tickers | bars "
                  f"{min(counts.values()):,}-{max(counts.values()):,} | "
                  f"{sample.index[0]} -> {sample.index[-1]}")
