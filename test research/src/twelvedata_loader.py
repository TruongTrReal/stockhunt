"""Download and cache OHLCV data for the S&P 500 universe from Twelve Data.

A drop-in alternative to `data_loader.py`: same `update_universe()` /
`load_universe()` interface, same DataFrame shape (Open/High/Low/Close/Volume on a
DatetimeIndex named Date), so downstream scripts only need their import swapped.

Three things about this API are load-bearing and easy to get wrong:

**`adjust=all` is mandatory.** The default response — and `adjust=splits` — is
split-adjusted but *not* dividend-adjusted. AAPL on 2015-01-02 comes back as
27.3325 by default and 24.19803 with `adjust=all`. Backtesting a total-return rule
on split-only prices silently strips every dividend out of the result.

**Errors arrive as HTTP 200.** A bad symbol or an exhausted quota is reported as
`{"status": "error"}` inside an otherwise successful response, per symbol. Nothing
raises unless you check the field.

**This cache is NOT interchangeable with `data/cache/`.** Twelve Data and yfinance
disagree on dividend-adjustment methodology: signals differ on only ~0.05% of
position-days, but final equity differs by a median of 0.9% and up to 18% on
dividend payers. Mixing the two, or comparing results derived from one against a
leaderboard derived from the other, manufactures differences that look like alpha.
That is why this writes to `data/cache_td/` rather than into the yfinance cache.
See `engine-bakeoff/ANALYSIS.md` finding 7.

Usage::

    python twelvedata_loader.py            # refresh the whole S&P 500 into data/cache_td/
    python twelvedata_loader.py AAPL MSFT  # just these
"""

import os
import sys
import time
from collections import deque
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from sp500_tickers import get_sp500_tickers

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache_td"
ENV_FILE = REPO_ROOT / ".env.local"

BASE_URL = "https://api.twelvedata.com/time_series"
DEFAULT_START = "2015-01-01"
# Twelve Data caps a single time_series response at 5000 rows per symbol; ~2,900
# daily bars since 2015 fits, but the default is 30, so it must be set explicitly.
OUTPUT_SIZE = 5000
# Symbols per request. The response is keyed by symbol when more than one is asked
# for, and each symbol carries its own status, so a batch degrades per-symbol
# rather than all-or-nothing.
#
# 20 is a hard cap, measured: 21 symbols returns `414 URI Too Long` while 20
# five-character symbols (a much longer URL) returns 200, so it is a symbol-count
# limit that merely reports itself as a URI-length error. `_download_batch` splits
# on 414 anyway, so this staying correct is not load-bearing — but a full S&P 500
# refresh at the wrong size fails every batch three times before telling you.
MAX_SYMBOLS_PER_REQUEST = 20
BATCH_SIZE = MAX_SYMBOLS_PER_REQUEST
# Pro plan allows 610 credits/minute and time_series bills 1 credit per symbol, so
# a 501-ticker refresh is ~501 credits — under the cap, but close enough that the
# limiter below is what keeps a re-run from tripping it.
CREDITS_PER_MINUTE = 550
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

_credit_log: deque[float] = deque()


def api_key() -> str:
    """Read the key from TWELVEDATA_API_KEY, else from a gitignored .env.local."""
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


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}.parquet"


def _api_symbol(ticker: str) -> str:
    """Project ticker -> Twelve Data symbol.

    Share classes are dash-separated in the Wikipedia/yfinance convention this
    project uses (`BRK-B`, `BF-B`) and dot-separated at Twelve Data (`BRK.B`,
    `BF.B`), which 404s rather than falling back. US equity tickers carry no other
    dashes, so the substitution is unambiguous. Cache files keep the project
    spelling so `get_sp500_tickers()` still lines up with what is on disk.
    """
    return ticker.replace("-", ".")


def _to_frame(values: list[dict]) -> pd.DataFrame:
    """One symbol's `values` array -> the project's standard OHLCV frame."""
    df = pd.DataFrame(values)
    df["Date"] = pd.to_datetime(df["datetime"])
    df = df.set_index("Date").sort_index()
    out = pd.DataFrame({
        "Open": pd.to_numeric(df["open"]),
        "High": pd.to_numeric(df["high"]),
        "Low": pd.to_numeric(df["low"]),
        "Close": pd.to_numeric(df["close"]),
        # int64 to match what the yfinance cache stores, so the two caches stay
        # structurally identical even though their values are not comparable.
        "Volume": pd.to_numeric(df["volume"]).fillna(0).astype("int64"),
    })
    return out.dropna()


def _download_batch(tickers: list[str], start: str,
                    end: str | None) -> dict[str, pd.DataFrame]:
    """Fetch up to MAX_SYMBOLS_PER_REQUEST symbols. Missing keys = failed symbols.

    A 414 means the batch exceeded the symbol cap, which retrying verbatim can
    never fix — so it splits and recurses instead. That makes the cap a
    performance knob rather than a correctness one.
    """
    if len(tickers) > MAX_SYMBOLS_PER_REQUEST:
        mid = len(tickers) // 2
        out = _download_batch(tickers[:mid], start, end)
        out.update(_download_batch(tickers[mid:], start, end))
        return out

    # Request under the API's spelling, but return under the project's.
    api_names = {_api_symbol(t): t for t in tickers}

    params = {
        "symbol": ",".join(api_names),
        "interval": "1day",
        "start_date": start,
        "outputsize": OUTPUT_SIZE,
        "adjust": "all",
        "order": "ASC",
        "apikey": api_key(),
    }
    if end:
        params["end_date"] = end

    _spend_credits(len(tickers))
    response = requests.get(BASE_URL, params=params, timeout=120)
    if response.status_code == 414 and len(tickers) > 1:
        mid = len(tickers) // 2
        out = _download_batch(tickers[:mid], start, end)
        out.update(_download_batch(tickers[mid:], start, end))
        return out
    response.raise_for_status()
    payload = response.json()

    # A single-symbol request returns the series flat; a multi-symbol request
    # returns it keyed by symbol. Normalise to the keyed shape.
    if len(api_names) == 1:
        payload = {next(iter(api_names)): payload}

    out: dict[str, pd.DataFrame] = {}
    for api_name, ticker in api_names.items():
        entry = payload.get(api_name)
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        values = entry.get("values")
        if not values:
            continue
        frame = _to_frame(values)
        if not frame.empty:
            out[ticker] = frame
    return out


def update_universe(
    tickers: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str | None = None,
    batch_size: int = BATCH_SIZE,
) -> list[str]:
    """Download fresh data for `tickers` (default: all S&P 500) and write to cache.

    Returns the list of tickers that failed after retries.
    """
    tickers = tickers or get_sp500_tickers()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    for batch in tqdm(batches, desc="Twelve Data batches"):
        pending = list(batch)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if not pending:
                break
            try:
                result = _download_batch(pending, start, end)
            except Exception as exc:  # network hiccup / 5xx, retry the whole batch
                if attempt == RETRY_ATTEMPTS:
                    print(f"Batch failed after {RETRY_ATTEMPTS} attempts: {exc}")
                    failed.extend(pending)
                    pending = []
                else:
                    time.sleep(RETRY_DELAY_SEC)
                continue

            for ticker, df in result.items():
                df.to_parquet(_cache_path(ticker))
            # Only symbols the API declined are retried; a symbol that simply does
            # not exist will exhaust its attempts and land in `failed`.
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
    requested = sys.argv[1:] or None
    failed = update_universe(tickers=requested)
    data = load_universe(requested)
    print(f"Loaded {len(data)} tickers from {CACHE_DIR}")
    if data:
        bars = {t: len(df) for t, df in data.items()}
        sample = next(iter(data.values()))
        print(f"bars per ticker: min={min(bars.values())} max={max(bars.values())}")
        print(f"date range: {sample.index[0].date()} -> {sample.index[-1].date()}")
