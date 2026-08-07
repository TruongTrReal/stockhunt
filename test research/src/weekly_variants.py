"""Compute indicators on WEEKLY and MONTHLY bars, held across the period.

Every signal in this project is computed on daily bars, and all 304 of them load
on one factor (mean pairwise correlation 0.740, effective independent bets 1.35,
no negative pair anywhere). Resampling the bars is the one remaining way to ask
a different question from the same library: a 13-week RSI is a bet on a quarter,
not a bet on a fortnight, and it cannot be expressed by any daily parameter.

Two things this could buy:
  independence  If weekly signals are genuinely less correlated with the daily
      champion, blending finally has something to work with - which is the thing
      every previous attempt lacked.
  turnover      A position held for a week or a month trades roughly 5x or 20x
      less than a daily one, attacking the cost wall (9-14bps breakeven) from
      the side no search has touched.

Bars are resampled per ticker (open=first, high=max, low=min, close=last,
volume=sum), indicators computed on that series, and the resulting position
forward-filled back onto the daily calendar - so the position is genuinely held
across the period rather than recomputed daily. The weekly close at week W is
known at W and the evaluator applies its usual one-bar lag on top, so no
lookahead is introduced.

    python weekly_variants.py
"""

import time

import numpy as np
import pandas as pd
from talib import abstract
from tqdm import tqdm

from broad_variants import CANDIDATES, rule_for
from data_loader import load_universe
from indicator_backtest import MIN_ROWS
from signal_tensor import CACHE_PATH, load
from sp500_tickers import get_sp500_tickers
from talib_signals import SELF_TREND_SMA_PERIOD, BENCHMARK_TICKER

FREQS = {"W": ("W-FRI", (4, 9, 13, 26, 52)),      # ~1m, 2m, 3m, 6m, 1y
         "M": ("ME", (3, 6, 12, 24))}              # ~1q, 2q, 1y, 2y
OUT_PATH = "../data/weekly_variant_tensor.npz"
AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


def build():
    t0 = time.time()
    base = load(CACHE_PATH)
    master = pd.DatetimeIndex(base["dates"])
    tickers = list(base["tickers"])
    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}

    specs = []
    for tag, (rule, periods) in FREQS.items():
        for fn in CANDIDATES:
            try:
                if "timeperiod" not in abstract.Function(fn).parameters:
                    continue
            except Exception:
                continue
            for p in periods:
                specs.append((f"{fn}_{p}{tag}", fn, p, rule_for(fn), rule))
    n_var, n_day, n_tic = len(specs), len(master), len(tickers)
    print(f"{n_var} variants ({sum(1 for s in specs if s[0].endswith('W'))} weekly, "
          f"{sum(1 for s in specs if s[0].endswith('M'))} monthly) "
          f"x {n_day} days x {n_tic} tickers = {n_var*n_day*n_tic/2**20:.0f} MiB")

    tensor = np.zeros((n_var, n_day, n_tic), dtype=np.int8)
    for i, ticker in enumerate(tqdm(tickers, desc="tickers")):
        df = data.get(ticker)
        if df is None:
            continue
        resampled = {}
        for freq in {s[4] for s in specs}:
            r = df.resample(freq).agg(AGG).dropna(how="any")
            if len(r) >= 60:
                resampled[freq] = r
        for j, (_, fn, p, rule, freq) in enumerate(specs):
            r = resampled.get(freq)
            if r is None:
                continue
            try:
                inputs = {k.lower(): r[k].to_numpy(dtype=float) for k in AGG}
                raw = abstract.Function(fn)(inputs, timeperiod=p)
                if isinstance(raw, list):
                    raw = raw[0]
                s = pd.Series(raw, index=r.index)
                close = r["Close"]
                if rule == "price":
                    pos = np.sign(close - s)
                elif rule == "zero":
                    pos = np.sign(s)
                elif rule == "midpoint":
                    lo, hi = (0, 100) if fn != "WILLR" else (-100, 0)
                    pos = np.sign(s - (lo + hi) / 2)
                else:
                    pos = np.sign(s - s.rolling(SELF_TREND_SMA_PERIOD).mean())
                # Forward-fill the period's position across its daily bars: the
                # position is HELD, not recomputed each day.
                daily = pos.fillna(0).reindex(master, method="ffill").fillna(0)
                tensor[j, :, i] = daily.to_numpy(dtype=np.int8)
            except Exception:
                continue

    live = [j for j in range(n_var) if np.abs(tensor[j]).sum() > 0]
    print(f"{len(live)} of {n_var} produced a signal")
    np.savez_compressed(OUT_PATH, tensor=tensor[live], returns=base["returns"],
                        closes=base["closes"], spy=base["spy"], dates=base["dates"],
                        tickers=base["tickers"],
                        names=np.array([specs[j][0] for j in live]))
    print(f"saved {OUT_PATH} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    build()
