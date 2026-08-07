"""Re-run the champion on a point-in-time universe, to size the survivorship bias.

Every result in this project uses today's 503 S&P 500 members run back to 2015.
742 names were in the index at some point over that window, so 239 are missing -
and they are the ones that were removed, which is exactly the cohort whose
absence flatters a dip buyer: you buy the dip and it always recovers, because
names that dipped and stayed down were never in the sample.

Of those 239, Yahoo still serves 116. The delisting dates say what they are:
107 have data through 2026 (demoted from the index but still trading - the
underperformer cohort) and only 9 actually stopped trading. The other 122 are
acquisitions and failures that Yahoo has purged, so they cannot be recovered at
all. This therefore measures a LOWER BOUND on the bias: the demoted names come
back in, the dead ones still cannot.

Membership is masked point-in-time - a stock is only held while it was actually
an index member - so the corrected run never trades a name the strategy could
not have known to own.

    python pit_backtest.py
"""

import time

import numpy as np
import pandas as pd
from tqdm import tqdm

import talib
from beat_buyhold_search import TRADING_DAYS, _transform
from data_loader import load_universe
from indicator_backtest import MIN_ROWS, _load_benchmark_close
from param_variants import _self_trend
from sharpe_search import sharpe_of
from sp500_tickers import get_sp500_tickers
from talib_signals import BENCHMARK_TICKER, generate_position

COST_BPS = 5.0
MEMBERSHIP = "../data/sp500_pit_membership.csv"
RECOVERED = "../data/sp500_pit_recovered.csv"


def stats(r):
    eq = np.cumprod(1 + np.clip(r, -0.999, None))
    yrs = len(r) / TRADING_DAYS
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    return {"cagr": float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else np.nan,
            "sharpe": sharpe_of(r), "maxdd": dd}


def main():
    t0 = time.time()
    current = get_sp500_tickers()
    recovered = list(pd.read_csv(RECOVERED).ticker)
    universe = sorted(set(current) | set(recovered))
    data = load_universe(universe)
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}
    tickers = sorted(data)
    cur_set = set(current)
    print(f"universe: {len(tickers)} tickers "
          f"({len(set(tickers)&cur_set)} current + {len(set(tickers)-cur_set)} recovered)")

    master = pd.DatetimeIndex(sorted(set().union(*[df.index for df in data.values()])))
    T, N = len(master), len(tickers)
    ret = np.full((T, N), np.nan, dtype=np.float32)
    for i, t in enumerate(tickers):
        ret[:, i] = data[t]["Close"].pct_change().reindex(master).to_numpy()
    valid = ~np.isnan(ret)
    R = np.where(valid, np.nan_to_num(ret), 0.0).astype(np.float32)

    # Point-in-time membership: monthly snapshots forward-filled to daily.
    pit = pd.read_csv(MEMBERSHIP, parse_dates=["date"])
    wide = (pit.assign(v=True).pivot_table(index="date", columns="ticker", values="v",
                                           aggfunc="first", fill_value=False)
            .reindex(columns=tickers, fill_value=False)
            .reindex(master.union(pit.date.unique())).ffill().reindex(master).fillna(False))
    member = wide.to_numpy(dtype=bool)
    print(f"membership mask: {member.sum(axis=1).min()}-{member.sum(axis=1).max()} names/day, "
          f"median {int(np.median(member.sum(axis=1)))}")

    bench = _load_benchmark_close()
    spy = bench.reindex(master).ffill().to_numpy(dtype=np.float32)
    sma100 = pd.Series(spy).rolling(100, min_periods=100).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma100), 0.0, (spy <= sma100).astype(np.float32))
    BEL = below[:, None]

    # Champion components (TA-Lib defaults) and C1 components (tuned).
    champ_raw = np.zeros((T, N), np.int8)
    c1_raw = np.zeros((T, N), np.int8)
    for i, t in enumerate(tqdm(tickers, desc="signals")):
        df = data[t]
        idx = df.index
        try:
            s = sum(-generate_position(n, df).reindex(master).fillna(0).to_numpy()
                    for n in ("ADOSC", "CDLCLOSINGMARUBOZU", "VAR"))
            champ_raw[:, i] = np.sign(s)
        except Exception:
            pass
        try:
            h, lo, c, v = (df[k].to_numpy(dtype=float) for k in ("High", "Low", "Close", "Volume"))
            legs = [
                np.sign(np.nan_to_num(talib.ADOSC(h, lo, c, v, fastperiod=5, slowperiod=30))),
                np.sign(np.nan_to_num(talib.ADOSC(h, lo, c, v, fastperiod=8, slowperiod=20))),
                _self_trend(talib.AD(h, lo, c, v), 10),
                _self_trend(talib.VAR(c, timeperiod=20), 60),
            ]
            s = sum(-pd.Series(x, index=idx).reindex(master).fillna(0).to_numpy() for x in legs)
            c1_raw[:, i] = np.sign(s)
        except Exception:
            pass

    books = {
        "BUYHOLD": np.ones((T, N), np.float32),
        "CHAMPION": (_transform(champ_raw, "defensive").astype(np.float32) - 0.5 * BEL),
        "C1": (_transform(c1_raw, "longonly").astype(np.float32) - 0.3 * BEL),
    }

    def run(w, mask, costs=True):
        w = (w * mask).astype(np.float32)
        held = np.vstack([np.zeros((1, N), np.float32), w[:-1]])
        m = valid & mask
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if costs:
            first = np.zeros((1, N), np.float32)
            out = out - (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n \
                * COST_BPS / 1e4
        return out

    cur_mask = np.zeros((T, N), bool)
    for i, t in enumerate(tickers):
        cur_mask[:, i] = t in cur_set

    print(f"\n{'book':<10}{'universe':<34}{'CAGR':>9}{'Sharpe':>9}{'maxDD':>9}")
    print("-" * 71)
    res = {}
    for label, mask, desc in (
        ("biased", cur_mask, "today's 503, full history (current)"),
        ("pit", member, "point-in-time membership, 619 names"),
    ):
        for name, w in books.items():
            r = run(w, mask, costs=(name != "BUYHOLD"))
            st = stats(r)
            res[(name, label)] = st
            print(f"{name:<10}{desc:<34}{st['cagr']:>9.2%}{st['sharpe']:>9.3f}{st['maxdd']:>9.2%}")
        print()

    print("Effect of correcting the universe (point-in-time minus biased):")
    for name in books:
        a, b = res[(name, "pit")], res[(name, "biased")]
        print(f"  {name:<10} CAGR {a['cagr']-b['cagr']:+7.2%}   Sharpe {a['sharpe']-b['sharpe']:+7.3f}"
              f"   maxDD {a['maxdd']-b['maxdd']:+7.2%}")
    for name in ("CHAMPION", "C1"):
        for lab in ("biased", "pit"):
            d = res[(name, lab)]["sharpe"] - res[("BUYHOLD", lab)]["sharpe"]
            print(f"  {name} edge over B&H on {lab:<7}: {d:+.3f} Sharpe")
    print(f"\n[{time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
