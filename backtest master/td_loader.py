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

from config import (CLASSES, ENV_FILE, TIMEFRAMES, cache_dir, safe_symbol,
                    window_spec)

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


def _request(symbols: list[str], interval: str, start: str, end: str,
             depth: int = 0) -> dict[str, pd.DataFrame]:
    """One batched request, bisecting on symbol-count overflow or a full response."""
    if len(symbols) > MAX_SYMBOLS_PER_REQUEST:
        mid = len(symbols) // 2
        out = _request(symbols[:mid], interval, start, end, depth)
        out.update(_request(symbols[mid:], interval, start, end, depth))
        return out

    api_names = {_api_symbol(s): s for s in symbols}
    params = {
        "symbol": ",".join(api_names), "interval": interval,
        "start_date": start, "end_date": end, "outputsize": OUTPUT_SIZE,
        "adjust": "all", "order": "ASC", "apikey": api_key(),
    }
    _spend_credits(len(symbols))
    response = requests.get(BASE_URL, params=params, timeout=180)
    if response.status_code == 414 and len(symbols) > 1:
        mid = len(symbols) // 2
        out = _request(symbols[:mid], interval, start, end, depth)
        out.update(_request(symbols[mid:], interval, start, end, depth))
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
        first = _request(saturated, interval, start, mid, depth + 1)
        second = _request(saturated, interval, mid, end, depth + 1)
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
    win = window_spec(asset_class, timeframe)
    interval = TIMEFRAMES[timeframe]["interval"]

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
                for symbol, frame in _request(batch, interval, start, end).items():
                    collected[symbol].append(frame)
                break
            except Exception as exc:
                if attempt == RETRY_ATTEMPTS:
                    print(f"  window {start}..{end} failed for {batch}: {exc}")
                else:
                    time.sleep(RETRY_DELAY_SEC)

    counts = {}
    for symbol, frames in collected.items():
        if not frames:
            print(f"  {symbol}: no data")
            continue
        # Windows are half-open but the API is inclusive at both ends, so adjacent
        # windows repeat a boundary bar.
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        df.to_parquet(out_dir / f"{safe_symbol(symbol)}.parquet")
        counts[symbol] = len(df)
    return counts


def load(asset_class: str, timeframe: str,
         symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Cache-only. Never downloads — call `fetch` first."""
    out_dir = cache_dir(asset_class, timeframe)
    wanted = symbols if symbols is not None else CLASSES[asset_class]["symbols"]
    data = {}
    for symbol in wanted:
        path = out_dir / f"{safe_symbol(symbol)}.parquet"
        if path.exists():
            data[symbol] = pd.read_parquet(path)
    return data


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
        for timeframe in args.timeframes:
            print(f"\n=== {asset_class} / {timeframe} ===")
            counts = fetch(asset_class, timeframe, args.symbols)
            if not counts:
                print("  nothing fetched")
                continue
            first = next(iter(counts))
            sample = load(asset_class, timeframe, [first])[first]
            print(f"  {len(counts)} symbols | bars {min(counts.values()):,}-"
                  f"{max(counts.values()):,} | {sample.index[0]} -> {sample.index[-1]}")


if __name__ == "__main__":
    main()
