"""Lever the volatility-timed combos to buy-and-hold's risk, and compare RETURN.

The six finalists beat buy-and-hold on Sharpe (0.54 vs 0.43) but earn less
(8.8-11.4% vs 11.67% CAGR) because they run 40-63% exposure. Sharpe is
scale-invariant, so a higher Sharpe at equal volatility must mean higher
arithmetic return - the question is whether that survives the three things
leverage actually costs:

  financing     borrowing the levered portion is not free. Modelled explicitly
                as a cash balance of (1 - |w|) per ticker: negative balances pay
                the rate, and - importantly - positive balances EARN it. These
                strategies are flat 40-60% of the time, so cash interest is a
                real part of their return that the unlevered comparison ignored.
  volatility    geometric return is roughly mu*L - sigma^2*L^2/2. Leverage
  drag          scales the mean linearly but the drag quadratically, so there is
                a leverage level beyond which CAGR falls.
  turnover      trading costs scale linearly with leverage; a 40x/yr book at
                1.6x leverage pays 1.6x the bill.

Leverage is solved on TRAIN (match buy-and-hold's realised volatility) and
applied unchanged to TEST. The rate is swept rather than assumed, since the
test window spans a 0% to 5.5% policy path.

    python leverage_test.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator
from signal_tensor import CACHE_PATH, load

OUT = "../data/leverage_results.csv"
COST_BPS = 5.0
RATES = (0.0, 0.02, 0.04, 0.05)       # annual financing / cash rate
MAX_LEVERAGE = 3.0


class LeveredBook:
    """Per-ticker levered book with an explicit cash balance."""

    def __init__(self, z, ev):
        self.ev = ev
        self.valid = ev.valid
        self.slices, self.keep = ev.slices, ev.keep
        self.R = np.where(self.valid, np.nan_to_num(z["returns"]), 0.0).astype(np.float32)
        self.n = {w: self.valid[s:e].sum(axis=0) for w, (s, e) in ev.slices.items()}

    def _held(self, w, s, e):
        if s > 0:
            return w[s - 1:e - 1]
        return np.vstack([np.zeros((1, w.shape[1]), w.dtype), w[:e - 1]])

    def daily(self, w, window, rate=0.0, cost_bps=COST_BPS):
        """Per-ticker daily total return: position P&L + cash interest - costs."""
        s, e = self.slices[window]
        held, m, R = self._held(w, s, e), self.valid[s:e], self.R[s:e]
        pnl = held * R
        cash = (1.0 - np.abs(held)) * (rate / TRADING_DAYS)
        first = w[s - 2:s - 1] if s >= 2 else np.zeros((1, w.shape[1]), w.dtype)
        traded = np.abs(np.diff(np.vstack([first, held]), axis=0))
        return np.where(m, pnl + cash - traded * cost_bps / 1e4, 0.0)

    def stats(self, w, window, rate=0.0, cost_bps=COST_BPS):
        s, e = self.slices[window]
        r = self.daily(w, window, rate, cost_bps)
        m = self.valid[s:e]
        n = np.maximum(self.n[window], 2).astype(np.float64)
        years = n / TRADING_DAYS
        lg = np.log1p(np.clip(r, -0.999, None))
        cagr = np.expm1(lg.sum(axis=0) / years)
        mean = (r * m).sum(axis=0) / n
        var = np.maximum(((r * r) * m).sum(axis=0) / n - mean * mean, 0)
        vol = np.sqrt(var * TRADING_DAYS)
        equity = np.cumprod(1 + np.where(m, r, 0.0), axis=0)
        dd = (equity / np.maximum.accumulate(equity, axis=0) - 1).min(axis=0)
        k = self.keep[window]
        return {"cagr": float(cagr[k].mean()), "vol": float(vol[k].mean()),
                "maxdd": float(dd[k].mean())}


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    lb = LeveredBook(z, ev)
    names = list(z["names"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), dtype=np.float32)

    bh_train = lb.stats(ones, "train")
    bh_test = lb.stats(ones, "test")
    print(f"Buy & hold   train: CAGR {bh_train['cagr']:.2%} vol {bh_train['vol']:.2%}   "
          f"test: CAGR {bh_test['cagr']:.2%} vol {bh_test['vol']:.2%} "
          f"maxDD {bh_test['maxdd']:.1%}\n")

    fin = pd.read_csv("../data/sharpe_finalists.csv")
    rows = []
    for _, row in fin.iterrows():
        atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                             for t in row["combo"].split(" + ")))
        pos = sp.build2(atoms, None, 1, row["transform"], row["gate"]
                        if "gate" in row else "none").astype(np.float32)

        # Leverage solved on TRAIN only: match buy-and-hold's realised vol.
        unlev = lb.stats(pos, "train")
        L = min(bh_train["vol"] / max(unlev["vol"], 1e-9), MAX_LEVERAGE)
        w = (L * pos).astype(np.float32)

        rec = {"combo": row["combo"], "transform": row["transform"],
               "leverage": L, "unlev_test_cagr": row["test_cagr"],
               "test_sharpe": row["test_sharpe"]}
        tr = lb.stats(w, "train", rate=0.04)
        rec["train_cagr_4pct"] = tr["cagr"]
        for rate in RATES:
            st = lb.stats(w, "test", rate=rate)
            rec[f"test_cagr_{int(rate*100)}pct"] = st["cagr"]
            if rate == 0.04:
                rec["test_vol"] = st["vol"]
                rec["test_maxdd"] = st["maxdd"]
        rows.append(rec)

    df = pd.DataFrame(rows).sort_values("test_cagr_4pct", ascending=False)
    df.to_csv(OUT, index=False)
    pd.set_option("display.width", 280)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 38)

    print(f"Levered to buy-and-hold's TRAIN volatility (leverage solved on train, "
          f"capped at {MAX_LEVERAGE}x), costs {COST_BPS:.0f}bps:\n")
    cols = ["combo", "transform", "leverage", "unlev_test_cagr"] + \
           [f"test_cagr_{int(r*100)}pct" for r in RATES] + \
           ["test_vol", "test_maxdd", "train_cagr_4pct"]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print(f"\nB&H test CAGR {bh_test['cagr']:.2%} at vol {bh_test['vol']:.2%}, "
          f"maxDD {bh_test['maxdd']:.1%}")
    for rate in RATES:
        c = f"test_cagr_{int(rate*100)}pct"
        print(f"  financing {rate:.0%}: {int((df[c] > bh_test['cagr']).sum())} of {len(df)} "
              f"beat B&H on test CAGR (best {df[c].max():.2%})")


if __name__ == "__main__":
    main()
