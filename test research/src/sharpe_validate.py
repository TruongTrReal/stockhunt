"""Adversarial validation of the Sharpe search survivors.

sharpe_search.py filters on "beats buy-and-hold Sharpe on train and on unseen
test". That is necessary but nowhere near sufficient, as the ~BETA candidate
showed: it cleared both, posted a Memmel z of +13.55, and was still worthless.
Each check below is one way a candidate that passes the search can still be
fake, and every survivor has to clear all of them.

  extra-lag     Re-run the strategy with one additional bar of delay. A real
                edge degrades; a one-day microstructure artifact collapses or
                reverses. ~BETA went from z +13.55 to -8.62 on this test alone.
  breakeven     The cost level at which the edge disappears. Reversal-flavoured
                signals in this project keep landing at 6-12bps, i.e. below
                realistic execution costs.
  robustness    Must beat B&H in BOTH train halves, not just on average.
  correlation   A near-1.0 correlation with buy-and-hold inflates the Memmel
                z: tiny differences get a tiny standard error. Reported so the
                statistic is read alongside its economic magnitude.
  multiplicity  With ~150k candidates searched, the significance bar is far
                above the usual 2. The expected max |z| under pure noise is
                printed for comparison.

    python sharpe_validate.py [--top N]
"""

import argparse
import math

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator
from beat_buyhold_search2 import Space2
from sharpe_search import OUT_CSV, SharpeEvaluator, memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/sharpe_validated.csv"
COST_LEVELS = (0, 5, 10, 20, 30, 50)


def breakeven_bps(gross, traded, bh_series):
    """Highest cost (bps) at which the strategy still beats B&H Sharpe."""
    lo, hi = 0.0, 200.0
    if sharpe_of(gross) <= sharpe_of(bh_series):
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if sharpe_of(gross - traded * mid / 1e4) > sharpe_of(bh_series):
            lo = mid
        else:
            hi = mid
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    bh = se.baseline
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    df = pd.read_csv(OUT_CSV)
    n_searched = 173_732
    # Rank by out-of-sample Sharpe, not by Memmel z. The z statistic divides by
    # the standard error of the difference, which collapses toward zero when a
    # candidate is nearly identical to buy-and-hold - so sorting by z puts
    # strategies that never trade (CDLCONCEALBABYSWALL and friends, scoring
    # 0.6402/0.4306, i.e. B&H to four decimals) at the very top with z ~ 19.
    n_all = len(df)
    df = df[(df.exposure_test < 0.98) & (df.test_sharpe > bh["test"] + 0.02) & df.robust]
    df = df.sort_values("test_sharpe", ascending=False)
    print(f"{n_all} survivors -> {len(df)} after dropping buy-and-hold look-alikes "
          f"(exposure >= 0.98 or margin < 0.02) and non-robust candidates")
    print(f"auditing top {min(args.top, len(df))} by TEST Sharpe")
    print(f"B&H Sharpe: train {bh['train']:.4f}  test {bh['test']:.4f}\n")

    s, e = se.slices["test"]
    m = se.valid[s:e]
    n_tick = np.maximum(m.sum(axis=1), 1)
    b = se.portfolio_returns(ones, "test")

    rows = []
    for _, row in df.head(args.top).iterrows():
        atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                             for t in row["combo"].split(" + ")))
        pos = sp.build2(atoms, None, 1, row["transform"], row["gate"])
        lag2 = np.vstack([np.zeros((1, N), np.int8), pos[:-1]])

        gross = se.portfolio_returns(pos, "test")
        held = se._held(pos, s, e)
        first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, N), pos.dtype)
        traded = (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n_tick
        net = gross - traded * 5 / 1e4

        rows.append({
            "combo": row["combo"], "transform": row["transform"], "gate": row["gate"],
            "train_sharpe": row["train_sharpe"], "test_sharpe": row["test_sharpe"],
            "robust": bool(row["robust"]),
            "test_sharpe_lag2": se.avg(lag2, "test"),
            "z_net": memmel_z(net, b)[0],
            "z_lag2": memmel_z(se.portfolio_returns(lag2, "test", costs=True), b)[0],
            "breakeven_bps": breakeven_bps(gross, traded, b),
            "turnover_yr": float(traded.mean() * 252),
            "corr_bh": float(np.corrcoef(net, b)[0, 1]),
            "exposure_test": row["exposure_test"],
        })

    out = pd.DataFrame(rows)
    # Survives everything: robust across both train halves, still beats B&H
    # after an extra bar of delay, and tradable above a realistic cost floor.
    out["PASSES"] = (out.robust & (out.test_sharpe_lag2 > bh["test"])
                     & (out.z_lag2 > 0) & (out.breakeven_bps >= 20))
    out = out.sort_values("z_net", ascending=False)
    out.to_csv(OUT, index=False)

    pd.set_option("display.width", 270)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 40)
    cols = ["combo", "transform", "gate", "robust", "test_sharpe", "test_sharpe_lag2",
            "z_net", "z_lag2", "breakeven_bps", "turnover_yr", "corr_bh", "PASSES"]
    print(out[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    exp_max = math.sqrt(2 * math.log(max(n_searched, 2)))
    print(f"\nExpected max |z| from ~{n_searched:,} pure-noise candidates: {exp_max:.2f}")
    print(f"{int(out.PASSES.sum())} of {len(out)} pass every check "
          f"(robust + survives extra lag + breakeven >= 20bps)")
    if out.PASSES.any():
        print("\nPASSING:")
        print(out[out.PASSES][cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
