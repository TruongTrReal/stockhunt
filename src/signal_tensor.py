"""Build and cache the full (indicator x day x ticker) position tensor.

Every combo search re-uses the same underlying positions, and generating them is
the expensive step (~231 indicators x ~501 tickers of TA-Lib calls). Computing
them once into an int8 tensor turns each subsequent strategy evaluation into
pure array math, which is what makes searching a large combo space feasible at
all - the alternative is regenerating identical signals for every candidate.

Positions are stored on a shared master date index (union of all tickers'
calendars) so any combination of indicators is elementwise-comparable without
realigning. Tickers absent on a given day get position 0 and are excluded from
that day's stats via the returns NaN mask, not treated as flat-but-listed.

    python signal_tensor.py          # build -> ../data/signal_tensor.npz
"""

import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from data_loader import load_universe
from indicator_backtest import MIN_ROWS, NEEDS_BENCHMARK, _load_benchmark_close
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER, generate_position, get_all_indicator_names

CACHE_PATH = "../data/signal_tensor.npz"


def build(path: str = CACHE_PATH) -> None:
    start = time.time()
    data = load_universe(get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}
    tickers = sorted(data)
    names = get_all_indicator_names()

    master = pd.DatetimeIndex(sorted(set().union(*[df.index for df in data.values()])))
    n_ind, n_day, n_tic = len(names), len(master), len(tickers)
    print(f"Building tensor: {n_ind} indicators x {n_day} days x {n_tic} tickers "
          f"({n_ind * n_day * n_tic / 1e6:.0f}M int8 = {n_ind * n_day * n_tic / 2**20:.0f} MiB)")

    # Returns are computed per ticker BEFORE reindexing, so a listing gap can't
    # be read as one giant multi-day price move.
    returns = np.full((n_day, n_tic), np.nan, dtype=np.float32)
    closes = np.full((n_day, n_tic), np.nan, dtype=np.float32)
    for i, ticker in enumerate(tickers):
        close = data[ticker]["Close"]
        returns[:, i] = close.pct_change().reindex(master).to_numpy()
        closes[:, i] = close.reindex(master).to_numpy()

    benchmark_close = _load_benchmark_close()
    spy = benchmark_close.reindex(master).ffill().to_numpy(dtype=np.float32)

    tensor = np.zeros((n_ind, n_day, n_tic), dtype=np.int8)
    failures = 0
    for j, name in enumerate(tqdm(names, desc="indicators")):
        kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
        for i, ticker in enumerate(tickers):
            try:
                pos = generate_position(name, data[ticker], **kwargs)
            except Exception:
                failures += 1
                continue
            tensor[j, :, i] = pos.reindex(master).fillna(0).to_numpy(dtype=np.int8)

    print(f"{failures} (indicator, ticker) pairs failed to generate - left flat")
    print(f"Saving to {path} ...")
    np.savez_compressed(
        path, tensor=tensor, returns=returns, closes=closes, spy=spy,
        dates=master.values.astype("datetime64[ns]"),
        tickers=np.array(tickers), names=np.array(names),
    )
    print(f"Done in {time.time() - start:.0f}s")


def load(path: str = CACHE_PATH) -> dict:
    z = np.load(path, allow_pickle=False)
    return {k: z[k] for k in z.files}


if __name__ == "__main__":
    build()
