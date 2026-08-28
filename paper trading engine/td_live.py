"""Twelve Data as a live bar source, and the Nautilus data client that wraps it.

**Why BARS are REST and only marks are streamed.** This file used to say the WebSocket
"is the right tool only if the study ever goes intraday", and the study has gone intraday
— `paper_config.MEMBER_TIMEFRAMES` now offers 1m. That did not move the bars, and the
reason is worth stating because it is the whole discipline of this desk:

*The stream delivers TICKS, and the research is built on the vendor's BARS.* The backtest
cache comes from `/time_series`, and `SandboxExecutionClient` prices every fill at the
signal bar's close, so the live record is comparable to the sheet only for as long as both
sides mean the same thing by "the bar". Bars aggregated here from ticks would be *this
desk's* bars — near enough to the vendor's to pass every eye test, different enough to
make the forward test measure something the research never scored, and nothing would say
so. So bars stay on `/time_series`, unchanged, and the socket is used for the two things
that need no bar definition at all: marking open positions continuously, and knowing
whether the feed is alive. That lives in `live_ws.py`, which owns the single upstream
connection.

What the stream prompted on the bar path was a measurement, not an aggregation. A
1-minute bar's close was timed stopping ~20 seconds after that bar's true close, so
`POLL_LAG` no longer has to be 90 seconds at `1m` — see `td_nautilus.POLL_LAG_BY_TF`,
which also records the two ways of measuring it wrong.

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

import paper_config

BASE_URL = "https://api.twelvedata.com"
OUTPUT_SIZE = 5000

# Vendor interval strings, and how long one bar lasts. The 4h duration is nominal: a US
# equity session is one 4h bar plus a ~2.5h stub, so the stub bar closes early. That only
# ever makes the freshness test stricter, which is the safe direction — and the same is
# true of every intraday timeframe below it, for the same reason.
#
# Taken from the engine's own `TIMEFRAMES` (re-exported by `paper_config`), which already
# names the vendor interval for each one.
# Restating them here is how this map and the engine's came to disagree about which
# timeframes exist at all.
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
INTERVALS = {
    tf: (spec["interval"], timedelta(seconds=int(tf[:-1]) * _UNIT_SECONDS[tf[-1]]))
    for tf, spec in paper_config.TIMEFRAMES.items()
}


def api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key.strip()
    env = paper_config.REPO / ".env.local"
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


def return_between(symbol: str, timeframe: str, start: datetime) -> float | None:
    """Percent move in `symbol` from the last bar at or before `start` to the latest close.

    This is what buy-and-hold did while the paper desk was stopped. The strategy earned 0
    over that window because it held nothing; the benchmark did not, and pretending
    otherwise would flatter every strategy through a drawdown it simply was not present
    for. Returns None when the window cannot be measured — the caller stores that as an
    unknown gap rather than as a zero.
    """
    _, span = INTERVALS[timeframe]
    elapsed = datetime.now(timezone.utc) - start
    bars = int(elapsed / span) + 5
    if bars < 2:
        return None
    df = fetch_bars(symbol, timeframe, n=min(bars, 5000))
    if df is None or len(df) < 2:
        return None
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    prior = df[idx <= start]
    if prior.empty:
        return None
    first = float(prior["Close"].iloc[-1])
    last = float(df["Close"].iloc[-1])
    if first <= 0:
        return None
    return round((last / first - 1.0) * 100.0, 6)


def is_market_open(symbol: str) -> bool | None:
    """Vendor's own session flag. None when it does not report one (e.g. crypto)."""
    r = requests.get(f"{BASE_URL}/quote", timeout=30,
                     params={"symbol": symbol, "apikey": api_key()})
    r.raise_for_status()
    return r.json().get("is_market_open")


# How much of one `/price` request the symbol list may fill. The vendor puts the batch in
# the QUERY STRING, so the ceiling is its gateway's URL limit and not anything it
# documents: at 125 symbols the desk's request was ~1.1 kB of URL and came back
# `414 URI Too Long` every single time. Both guards are needed — a count alone still
# overflows on crypto pairs, which cost 11 characters each once `/` is percent-encoded,
# and a length alone would send one enormous request the day the vendor raises the limit.
MAX_PRICE_SYMBOLS = 50
MAX_PRICE_CHARS = 400


def _price_chunks(symbols: list[str]) -> list[list[str]]:
    """Split a symbol list into requests that will fit in a URL."""
    chunks: list[list[str]] = []
    run: list[str] = []
    run_len = 0
    for sym in symbols:
        cost = len(sym) * 3 + 3               # worst-case percent-encoding, plus a comma
        if run and (len(run) >= MAX_PRICE_SYMBOLS or run_len + cost > MAX_PRICE_CHARS):
            chunks.append(run)
            run, run_len = [], 0
        run.append(sym)
        run_len += cost
    if run:
        chunks.append(run)
    return chunks


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Current price for many symbols, in as few calls as will fit in a URL.

    `/price` is batched — a comma-separated list returns a dict keyed by symbol. A
    single-symbol request returns the bare object instead of a dict of them, so both shapes
    are handled.

    **It is batched into the URL, which is why this chunks.** The docstring here used to
    promise "one call", and that held while the desk marked 33 instruments. The point-in-time
    top 100 took it to ~125, the request grew past the vendor's URL limit, and mark-to-market
    then failed with `414 URI Too Long` on every pass — 1,035 failures and no successes in a
    day, while the desk went on filling orders against prices it could no longer refresh. A
    batch endpoint has a size, and nothing here was bounding it.

    **A chunk that fails does not take the others down with it.** Partial marks are the
    point: one unpriceable symbol used to mean the whole book went unpriced. Only a total
    failure raises, so the caller's "will retry" still fires for a real outage rather than
    for one bad ticker.

    This exists for mark-to-market only, never for signals. Positions are decided on closed
    bars; an intraday price is for showing what the open position is worth right now, and
    feeding it to a rule would trade a bar that has not finished forming.
    """
    if not symbols:
        return {}
    out: dict[str, float] = {}
    failures: list[Exception] = []
    chunks = _price_chunks(symbols)
    for chunk in chunks:
        try:
            r = requests.get(f"{BASE_URL}/price", timeout=30,
                             params={"symbol": ",".join(chunk), "apikey": api_key()})
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:               # noqa: BLE001 - reported below, not swallowed
            failures.append(exc)
            continue
        if "price" in payload:                 # single symbol: bare object
            payload = {chunk[0]: payload}
        for sym, v in payload.items():
            try:
                out[sym] = float(v["price"])
            except (TypeError, ValueError, KeyError):
                continue
    if failures and len(failures) == len(chunks):
        raise failures[0]
    if failures:
        print(f"fetch_prices: {len(failures)} of {len(chunks)} chunks failed "
              f"({failures[0]}); marked {len(out)} of {len(symbols)} symbols", flush=True)
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
    print(f"warmup window in use: {paper_config.DEFAULT_WINDOW_BARS} bars "
          f"(measured worst case {paper_config.MEASURED_WINDOW_BARS})")
    for tf in paper_config.FORWARD_TIMEFRAMES:
        for sym in ["SOXL", "TQQQ", "BTC/USD"]:
            try:
                df = fetch_bars(sym, tf, n=paper_config.DEFAULT_WINDOW_BARS)
                raw = fetch_bars(sym, tf, n=5, drop_forming=False)
                dropped = len(raw) and (len(df) == 0 or raw.index[-1] > df.index[-1])
                print(f"  {sym:9s} {tf:3s} {len(df):5d} closed bars | "
                      f"last close {df.index[-1]} @ {df['Close'].iloc[-1]:.4f}"
                      f"{'  [forming bar dropped]' if dropped else ''}")
            except Exception as exc:
                print(f"  {sym:9s} {tf:3s} FAILED: {exc}")


if __name__ == "__main__":
    _smoke()


# Twelve Data accepts a comma-separated symbol list on `/time_series` and answers with a
# dict keyed by symbol. Chunked below this, because a very long list makes one request
# whose failure loses every symbol in it — and because the vendor caps the list length.
BATCH_SYMBOLS = 50


def fetch_bars_many(symbols: list[str], timeframe: str, n: int = 1500,
                    drop_forming: bool = True,
                    country: str | None = None) -> dict[str, pd.DataFrame]:
    """The last `n` closed bars for MANY symbols, in as few requests as possible.

    `fetch_bars` asks for one symbol per request, which is fine for a 33-instrument desk
    and stops being fine at a hundred: every subscription polls at the same bar close, so
    a hundred names is a hundred simultaneous requests in the second after the bell.

    Credits are counted per symbol either way — batching does not make the data cheaper —
    but it collapses the request COUNT, which is what a rate limiter measures and what
    turns a burst into a queue of failures.

    A symbol that errors is omitted from the result rather than raising, because one
    delisted or mistyped name must not cost the other ninety-nine their bars. The caller
    sees a short dict and can say which are missing.
    """
    interval, duration = INTERVALS[timeframe]
    out: dict[str, pd.DataFrame] = {}
    now = datetime.now(timezone.utc)

    for i in range(0, len(symbols), BATCH_SYMBOLS):
        chunk = symbols[i:i + BATCH_SYMBOLS]
        params = {
            "symbol": ",".join(chunk), "interval": interval,
            "outputsize": min(n + 2, OUTPUT_SIZE), "adjust": "all",
            "order": "ASC", "apikey": api_key(),
        }
        if country:
            params["country"] = country
        r = requests.get(f"{BASE_URL}/time_series", timeout=120, params=params)
        r.raise_for_status()
        payload = r.json()

        # One symbol comes back as the bare object, several as a dict keyed by symbol —
        # the same asymmetry `fetch_prices` handles, and the same trap: a one-name chunk
        # would otherwise be read as a dict of symbols called "meta" and "values".
        if "values" in payload or payload.get("status") == "error":
            payload = {chunk[0]: payload}

        for symbol in chunk:
            block = payload.get(symbol)
            if not isinstance(block, dict) or "values" not in block:
                continue
            df = _to_frame(block["values"])
            if drop_forming and len(df):
                last_open = df.index[-1]
                if last_open.tzinfo is None:
                    last_open = last_open.tz_localize("UTC")
                if now < last_open + duration:
                    df = df.iloc[:-1]
            out[symbol] = df.tail(n)
    return out
