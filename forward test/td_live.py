"""Twelve Data as a live bar source, and the Nautilus data client that wraps it.

**Why REST polling and not the WebSocket.** The forward test is 1d and 4h, so a strategy
makes one or two decisions per day. A tick stream would deliver thousands of messages to
throw away, plus reconnects, heartbeats and gap recovery to get wrong. One `/time_series`
call per symbol shortly after each bar close costs 1 credit against a 610/minute budget
and cannot desynchronise. The WebSocket (verified working on this key for both equities
and Binance-sourced crypto) is the right tool only if the study ever goes intraday.

**Two vendor behaviours this has to defend against.**

*The forming bar.* `/time_series` returns the current, still-open bar as its most recent
row. Acting on it is look-ahead of the worst kind — the close changes after you trade. So
every read discards the newest row unless the vendor's own clock says that bar's interval
has fully elapsed. This is the single most important line in the file.

*The frozen tick timestamp.* On the WebSocket, `timestamp` repeats across a burst of ticks
— it is the bar stamp, not the tick time. Nothing here depends on it, but any future
streaming path must stamp arrival locally instead.

The Nautilus side is a `LiveMarketDataClient` that answers `_request_bars` from the vendor
(the strategy's warmup) and services `_subscribe_bars` by polling on the bar cadence. That
keeps `backtest -> paper -> live` one code path: only the execution client changes.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import fwd_config

BASE_URL = "https://api.twelvedata.com"
OUTPUT_SIZE = 5000

# Vendor interval strings, and how long one bar lasts. The 4h duration is nominal: a US
# equity session is one 4h bar plus a ~2.5h stub, so the stub bar closes early. That only
# ever makes the freshness test stricter, which is the safe direction.
INTERVALS = {"1d": ("1day", timedelta(days=1)), "4h": ("4h", timedelta(hours=4))}


def api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    env = fwd_config.REPO / ".env.local"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TWELVEDATA_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("No Twelve Data API key (env TWELVEDATA_API_KEY or .env.local)")


def _to_frame(values: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    cols = {"open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"}
    for src, dst in cols.items():
        df[dst] = pd.to_numeric(df[src], errors="coerce") if src in df else float("nan")
    return df[list(cols.values())]


def fetch_bars(symbol: str, timeframe: str, n: int = 1500,
               drop_forming: bool = True) -> pd.DataFrame:
    """The last `n` CLOSED bars for one symbol.

    With `drop_forming` the newest row is discarded unless a full interval has elapsed
    since it opened. Trading the forming bar would use a close that has not happened yet.
    """
    interval, duration = INTERVALS[timeframe]
    r = requests.get(f"{BASE_URL}/time_series", timeout=60, params={
        "symbol": symbol, "interval": interval, "outputsize": min(n + 2, OUTPUT_SIZE),
        "adjust": "all", "order": "ASC", "apikey": api_key(),
    })
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") == "error" or "values" not in payload:
        raise RuntimeError(f"{symbol} {timeframe}: {payload}")

    df = _to_frame(payload["values"])
    if drop_forming and len(df):
        last_open = df.index[-1]
        if last_open.tzinfo is None:
            last_open = last_open.tz_localize("UTC")
        if datetime.now(timezone.utc) < last_open + duration:
            df = df.iloc[:-1]
    return df.tail(n)


def latest_closed_bar(symbol: str, timeframe: str) -> pd.Series | None:
    df = fetch_bars(symbol, timeframe, n=1)
    return df.iloc[-1] if len(df) else None


def is_market_open(symbol: str) -> bool | None:
    """Vendor's own session flag. None when it does not report one (e.g. crypto)."""
    r = requests.get(f"{BASE_URL}/quote", timeout=30,
                     params={"symbol": symbol, "apikey": api_key()})
    r.raise_for_status()
    return r.json().get("is_market_open")


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Current price for many symbols in one call.

    `/price` is batched — a comma-separated list returns a dict keyed by symbol — which
    matters when 33 instruments need marking every minute. A single-symbol request returns
    the bare object instead of a dict of them, so both shapes are handled.

    This exists for mark-to-market only, never for signals. Positions are decided on closed
    bars; an intraday price is for showing what the open position is worth right now, and
    feeding it to a rule would trade a bar that has not finished forming.
    """
    if not symbols:
        return {}
    r = requests.get(f"{BASE_URL}/price", timeout=30,
                     params={"symbol": ",".join(symbols), "apikey": api_key()})
    r.raise_for_status()
    payload = r.json()
    if "price" in payload:                       # single symbol: bare object
        payload = {symbols[0]: payload}
    out = {}
    for sym, v in payload.items():
        try:
            out[sym] = float(v["price"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


# --------------------------------------------------------------------- Nautilus adapter

def build_data_client(*args, **kwargs):
    """Import the Nautilus client lazily.

    `nautilus_trader` lives in its own venv (`.venv-nautilus`) because it pins numpy and
    pyarrow against the rest of the repo. Importing it at module scope would make this
    file unusable from the research venv, where `fetch_bars` is perfectly useful on its
    own — `parity_live.py` and the smoke test below both run without Nautilus installed.
    """
    from td_nautilus import TwelveDataLiveClient      # noqa: PLC0415
    return TwelveDataLiveClient(*args, **kwargs)


def _smoke() -> None:
    """Prove the vendor path end to end without touching Nautilus."""
    print(f"warmup window in use: {fwd_config.DEFAULT_WINDOW_BARS} bars "
          f"(measured worst case {fwd_config.MEASURED_WINDOW_BARS})")
    for tf in fwd_config.FORWARD_TIMEFRAMES:
        for sym in ["SOXL", "TQQQ", "BTC/USD"]:
            try:
                df = fetch_bars(sym, tf, n=fwd_config.DEFAULT_WINDOW_BARS)
                raw = fetch_bars(sym, tf, n=5, drop_forming=False)
                dropped = len(raw) and (len(df) == 0 or raw.index[-1] > df.index[-1])
                print(f"  {sym:9s} {tf:3s} {len(df):5d} closed bars | "
                      f"last close {df.index[-1]} @ {df['Close'].iloc[-1]:.4f}"
                      f"{'  [forming bar dropped]' if dropped else ''}")
            except Exception as exc:
                print(f"  {sym:9s} {tf:3s} FAILED: {exc}")


if __name__ == "__main__":
    _smoke()
