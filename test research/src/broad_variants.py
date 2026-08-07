"""Sweep parameters across MANY TA-Lib functions, not just the champion's five.

param_variants.py swept only ADOSC/AD/VAR/STDDEV/CDLCLOSINGMARUBOZU, so its 168
atoms averaged 0.805 pairwise correlation - unsurprising when they are all
variants of five base functions. The interesting question is whether parameter
diversity ACROSS functions breaks that: a 3-period RSI and a 50-period RSI are
bets on different horizons, and horizon is the one axis that might produce
genuinely independent signals from this library.

Each function keeps the rule family talib_signals assigns it (price crossover,
zero line, midpoint oscillator, or self-trend), so a swept variant is the same
signal the project already uses, just at a different speed - not a new rule
smuggled in under an old name.

    python broad_variants.py
"""

import time

import numpy as np
import pandas as pd
import talib
from talib import abstract
from tqdm import tqdm

from data_loader import load_universe
from indicator_backtest import MIN_ROWS
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import (
    MIDPOINT_OSC, PRICE_CROSSOVER, SELF_TREND, SELF_TREND_SMA_PERIOD, ZERO_LINE,
    BENCHMARK_TICKER,
)

OUT_PATH = "../data/broad_variant_tensor.npz"
PERIODS = (3, 5, 9, 14, 25, 50, 100, 200)

# Single-output functions that take a plain `timeperiod`, grouped by the rule
# talib_signals applies to them. Anything needing several series or multiple
# parameters is left out - the point is speed diversity, not new rules.
CANDIDATES = [
    "RSI", "CCI", "CMO", "MFI", "WILLR", "MOM", "ROC", "ROCP", "TRIX",
    "ADX", "ADXR", "DX", "AROONOSC", "ATR", "NATR", "STDDEV", "VAR",
    "LINEARREG_SLOPE", "LINEARREG_ANGLE", "LINEARREG", "TSF", "CORREL",
    "SMA", "EMA", "WMA", "DEMA", "TEMA", "TRIMA", "KAMA", "MIDPOINT", "MIDPRICE",
    "BETA", "MINUS_DI", "PLUS_DI", "SUM", "MAX", "MIN", "AVGDEV",
]


def rule_for(name):
    if name in PRICE_CROSSOVER:
        return "price"
    if name in ZERO_LINE:
        return "zero"
    if name in MIDPOINT_OSC:
        return "midpoint"
    return "selftrend"


def build():
    t0 = time.time()
    base = load(CACHE_PATH)
    master = pd.DatetimeIndex(base["dates"])
    tickers = list(base["tickers"])
    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    specs, skipped = [], []
    for fn in CANDIDATES:
        try:
            f = abstract.Function(fn)
        except Exception:
            skipped.append(fn)
            continue
        if "timeperiod" not in f.parameters:
            skipped.append(fn)
            continue
        for p in PERIODS:
            specs.append((f"{fn}_{p}", fn, p, rule_for(fn)))
    print(f"{len(specs)} variants from {len(CANDIDATES)-len(skipped)} functions "
          f"(skipped {skipped})")

    n_var, n_day, n_tic = len(specs), len(master), len(tickers)
    print(f"tensor {n_var} x {n_day} x {n_tic} = {n_var*n_day*n_tic/2**20:.0f} MiB int8")
    tensor = np.zeros((n_var, n_day, n_tic), dtype=np.int8)

    for i, ticker in enumerate(tqdm(tickers, desc="tickers")):
        df = data.get(ticker)
        if df is None:
            continue
        inputs = {k.lower(): df[k].to_numpy(dtype=float)
                  for k in ("Open", "High", "Low", "Close", "Volume")}
        inputs["volume"] = inputs["volume"].astype(float)
        idx = df.index
        close = pd.Series(inputs["close"], index=idx)
        for j, (_, fn, p, rule) in enumerate(specs):
            try:
                raw = abstract.Function(fn)(inputs, timeperiod=p)
                if isinstance(raw, list):
                    raw = raw[0]
                s = pd.Series(raw, index=idx)
                if rule == "price":
                    pos = np.sign(close - s)
                elif rule == "zero":
                    pos = np.sign(s)
                elif rule == "midpoint":
                    lo, hi = (0, 100) if fn != "WILLR" else (-100, 0)
                    pos = np.sign(s - (lo + hi) / 2)
                else:
                    pos = np.sign(s - s.rolling(SELF_TREND_SMA_PERIOD).mean())
                tensor[j, :, i] = (pos.fillna(0).reindex(master).fillna(0)
                                   .to_numpy(dtype=np.int8))
            except Exception:
                continue

    live = [j for j in range(n_var) if np.abs(tensor[j]).sum() > 0]
    print(f"{len(live)} of {n_var} variants produced a signal")
    tensor = tensor[live]
    names = np.array([specs[j][0] for j in live])
    np.savez_compressed(OUT_PATH, tensor=tensor, returns=base["returns"],
                        closes=base["closes"], spy=base["spy"], dates=base["dates"],
                        tickers=base["tickers"], names=names)
    print(f"saved {OUT_PATH} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    build()
