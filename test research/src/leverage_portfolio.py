"""Lever the volatility-timed book at PORTFOLIO level and compare return.

leverage_test.py levered each ticker's $10k sleeve independently, which is the
project's convention but throws away diversification: the strategy's volatility
reduction is partly idiosyncratic, and averaging per-ticker CAGR never lets
those cancel. Net of costs the gap is much wider on the aggregate book
(portfolio Sharpe 0.99 vs 0.84) than per sleeve, so the aggregate is where a
volatility-matched comparison belongs - and it is also what anyone would
actually trade.

Construction: hold the equal-weight strategy book, lever it by a constant chosen
on TRAIN to match buy-and-hold's TRAIN volatility, and carry an explicit cash
balance so leverage pays financing and idle capital earns interest. Costs scale
with leverage. Everything is reported against the same equal-weight buy-and-hold
portfolio.

    python leverage_portfolio.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/leverage_portfolio_results.csv"
COST_BPS = 5.0
RATES = (0.0, 0.02, 0.04, 0.05)
MAX_LEVERAGE = 3.0


def curve_stats(daily):
    """CAGR / vol / maxDD / Sharpe of a daily simple-return series."""
    eq = np.cumprod(1 + np.clip(daily, -0.999, None))
    yrs = len(daily) / TRADING_DAYS
    return {
        "cagr": float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else float("nan"),
        "vol": float(daily.std(ddof=1) * np.sqrt(TRADING_DAYS)),
        "maxdd": float((eq / np.maximum.accumulate(eq) - 1).min()),
        "sharpe": sharpe_of(daily),
    }


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    def book(pos, window, lev=1.0, rate=0.0, cost_bps=COST_BPS):
        """Equal-weight portfolio daily return, levered, with a cash balance."""
        s, e = se.slices[window]
        held, m, R = se._held(pos, s, e), se.valid[s:e], se.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        pnl = lev * ((held * R) * m).sum(axis=1) / n
        exposure = lev * (np.abs(held) * m).sum(axis=1) / n
        first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, N), pos.dtype)
        traded = lev * (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n
        return pnl + (1.0 - exposure) * (rate / TRADING_DAYS) - traded * cost_bps / 1e4

    bh_tr = curve_stats(book(ones, "train"))
    bh_te = curve_stats(book(ones, "test"))
    print(f"Buy & hold portfolio   train: CAGR {bh_tr['cagr']:.2%} vol {bh_tr['vol']:.2%}   "
          f"test: CAGR {bh_te['cagr']:.2%} vol {bh_te['vol']:.2%} "
          f"maxDD {bh_te['maxdd']:.1%} Sharpe {bh_te['sharpe']:.3f}\n")

    fin = pd.read_csv("../data/sharpe_finalists.csv")
    rows = []
    for _, row in fin.iterrows():
        atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                             for t in row["combo"].split(" + ")))
        pos = sp.build2(atoms, None, 1, row["transform"], "none")

        # Leverage from TRAIN volatility only.
        unlev_tr = curve_stats(book(pos, "train", rate=0.04))
        L = min(bh_tr["vol"] / max(unlev_tr["vol"], 1e-9), MAX_LEVERAGE)

        rec = {"combo": row["combo"], "transform": row["transform"], "leverage": L}
        tr = curve_stats(book(pos, "train", lev=L, rate=0.04))
        rec["train_cagr_4pct"] = tr["cagr"]
        for rate in RATES:
            st = curve_stats(book(pos, "test", lev=L, rate=rate))
            rec[f"test_cagr_{int(rate*100)}pct"] = st["cagr"]
            if rate == 0.04:
                rec.update({"test_vol": st["vol"], "test_maxdd": st["maxdd"],
                            "test_sharpe": st["sharpe"]})
        rows.append(rec)

    df = pd.DataFrame(rows).sort_values("test_cagr_4pct", ascending=False)
    df.to_csv(OUT, index=False)
    pd.set_option("display.width", 280)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 38)

    cols = ["combo", "transform", "leverage"] + \
           [f"test_cagr_{int(r*100)}pct" for r in RATES] + \
           ["test_vol", "test_maxdd", "test_sharpe", "train_cagr_4pct"]
    print(f"Levered to B&H train volatility (cap {MAX_LEVERAGE}x), {COST_BPS:.0f}bps costs:\n")
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print(f"\nB&H test: CAGR {bh_te['cagr']:.2%}  vol {bh_te['vol']:.2%}  "
          f"maxDD {bh_te['maxdd']:.1%}  Sharpe {bh_te['sharpe']:.3f}")
    for rate in RATES:
        c = f"test_cagr_{int(rate*100)}pct"
        n_beat = int((df[c] > bh_te["cagr"]).sum())
        print(f"  financing {rate:.0%}: {n_beat} of {len(df)} beat B&H test CAGR "
              f"(best {df[c].max():.2%}, excess {df[c].max()-bh_te['cagr']:+.2%})")


if __name__ == "__main__":
    main()
