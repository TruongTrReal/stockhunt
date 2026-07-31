"""Build parameter-swept variants of the winning combo family.

Every search so far explored WHICH indicators to combine while treating each one
as a fixed atom at TA-Lib's default parameters. The champion is
`~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR`, and not one of its parameters has ever
been fitted:

  ADOSC   defaults to fastperiod=3, slowperiod=10
  VAR     defaults to timeperiod=5
  STDDEV  defaults to timeperiod=5
  the self-trend comparison window is SELF_TREND_SMA_PERIOD = 20, a single
    global constant that decides what "above its own average" means for the
    entire family
  candlestick patterns are held for PATTERN_HOLD_DAYS = 5

Those five numbers control how the champion behaves and none were chosen by
anything but TA-Lib's defaults. That is a whole dimension the beam search never
touched, and unlike another sweep over the same 231 atoms it is genuinely
unexplored.

Emits a tensor in the same format as signal_tensor.py so the existing search and
evaluation code can consume it unchanged.

    python param_variants.py
"""

import time

import numpy as np
import pandas as pd
import talib
from tqdm import tqdm

from data_loader import load_universe
from indicator_backtest import MIN_ROWS
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER

OUT_PATH = "../data/param_variant_tensor.npz"

ADOSC_PAIRS = [(2, 10), (3, 10), (3, 15), (3, 20), (5, 15), (5, 20), (5, 30), (8, 20), (8, 30)]
VAR_PERIODS = (5, 10, 20, 40)
STDDEV_PERIODS = (5, 10, 20, 40)
SMA_PERIODS = (10, 20, 40, 60)          # the self-trend comparison window
PATTERN_HOLDS = (2, 5, 10)


def _self_trend(series: np.ndarray, sma_period: int) -> np.ndarray:
    """Long when the series is above its own trailing SMA, short below - the
    same generic rule talib_signals uses, with the window exposed."""
    s = pd.Series(series)
    return np.sign(s - s.rolling(sma_period).mean()).fillna(0).to_numpy(dtype=np.int8)


def build():
    t0 = time.time()
    base = load(CACHE_PATH)
    master = pd.DatetimeIndex(base["dates"])
    tickers = list(base["tickers"])

    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    specs = []
    # ADOSC lives in talib_signals' ZERO_LINE family: the real rule is the sign
    # of the raw oscillator, NOT "above its own SMA". Sweeping it under the
    # self-trend rule would have searched a different signal while labelling it
    # as the champion's component. Both forms are emitted, correctly named.
    for f, s in ADOSC_PAIRS:
        specs.append((f"ADOSC_{f}_{s}_zero", ("adosc_zero", f, s, None)))
        for p in SMA_PERIODS:
            specs.append((f"ADOSCsma_{f}_{s}_sma{p}", ("adosc", f, s, p)))
    for p in SMA_PERIODS:
        specs.append((f"AD_sma{p}", ("ad", None, None, p)))
    for tp in VAR_PERIODS:
        for p in SMA_PERIODS:
            specs.append((f"VAR_{tp}_sma{p}", ("var", tp, None, p)))
    for tp in STDDEV_PERIODS:
        for p in SMA_PERIODS:
            specs.append((f"STDDEV_{tp}_sma{p}", ("stddev", tp, None, p)))
    for h in PATTERN_HOLDS:
        specs.append((f"CDLCLOSINGMARUBOZU_hold{h}", ("cdl", h, None, None)))

    n_var, n_day, n_tic = len(specs), len(master), len(tickers)
    print(f"{n_var} parameter variants x {n_day} days x {n_tic} tickers "
          f"({n_var*n_day*n_tic/2**20:.0f} MiB int8)")

    tensor = np.zeros((n_var, n_day, n_tic), dtype=np.int8)
    for i, ticker in enumerate(tqdm(tickers, desc="tickers")):
        df = data.get(ticker)
        if df is None:
            continue
        o = df["Open"].to_numpy(dtype=float)
        h = df["High"].to_numpy(dtype=float)
        lo = df["Low"].to_numpy(dtype=float)
        c = df["Close"].to_numpy(dtype=float)
        v = df["Volume"].to_numpy(dtype=float)
        idx = df.index

        # Raw series are computed once per (function, parameter) pair and reused
        # across every SMA window, which is the bulk of the saving here.
        raw_cache = {}
        for j, (_, (kind, a, b, p)) in enumerate(specs):
            try:
                if kind == "cdl":
                    sig = pd.Series(np.sign(talib.CDLCLOSINGMARUBOZU(o, h, lo, c)), index=idx)
                    pos = sig.replace(0, np.nan).ffill(limit=a).fillna(0).to_numpy(dtype=np.int8)
                elif kind == "adosc_zero":
                    key = ("adosc", a, b)
                    if key not in raw_cache:
                        raw_cache[key] = talib.ADOSC(h, lo, c, v, fastperiod=a, slowperiod=b)
                    pos = np.sign(np.nan_to_num(raw_cache[key])).astype(np.int8)
                else:
                    key = (kind, a, b)
                    if key not in raw_cache:
                        if kind == "adosc":
                            raw_cache[key] = talib.ADOSC(h, lo, c, v, fastperiod=a, slowperiod=b)
                        elif kind == "ad":
                            raw_cache[key] = talib.AD(h, lo, c, v)
                        elif kind == "var":
                            raw_cache[key] = talib.VAR(c, timeperiod=a)
                        else:
                            raw_cache[key] = talib.STDDEV(c, timeperiod=a)
                    pos = _self_trend(raw_cache[key], p)
            except Exception:
                continue
            tensor[j, :, i] = pd.Series(pos, index=idx).reindex(master).fillna(0).to_numpy(np.int8)

    np.savez_compressed(
        OUT_PATH, tensor=tensor, returns=base["returns"], closes=base["closes"],
        spy=base["spy"], dates=base["dates"], tickers=base["tickers"],
        names=np.array([n for n, _ in specs]),
    )
    print(f"saved {OUT_PATH} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    build()
