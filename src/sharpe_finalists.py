"""Final report on the combos that beat buy-and-hold on risk-adjusted return.

Reports the survivors of sharpe_validate.py's audit with the figures needed to
judge them as strategies rather than as leaderboard rows: Sharpe and CAGR in
every window, exposure, turnover, and the cost level at which the edge
disappears.

The cost column is the whole story. These clear a 5bps round-trip - defensible
for institutional execution in S&P 500 large caps - and fail by 20bps. They are
not retail-tradable, and the honest headline is "beats buy-and-hold on Sharpe
at institutional cost levels", not "beats buy-and-hold".

    python sharpe_finalists.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/sharpe_finalists.csv"
Z_BAR = 4.91          # expected max |z| from 173,732 pure-noise candidates
COST_LEVELS = (0, 2, 5, 10, 20)


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    bh = se.baseline
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    aud = pd.read_csv("../data/sharpe_validated.csv")
    # Keep only what clears the multiplicity bar both as traded and with an
    # extra bar of delay; the lag condition is what separates a real edge from
    # a one-day execution artifact.
    aud = aud[(aud.z_net > Z_BAR) & (aud.z_lag2 > Z_BAR) & aud.robust]
    aud = aud.sort_values("test_sharpe", ascending=False)

    s, e = se.slices["test"]
    m = se.valid[s:e]
    n_tick = np.maximum(m.sum(axis=1), 1)
    b = se.portfolio_returns(ones, "test")

    rows = []
    for _, row in aud.iterrows():
        atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                             for t in row["combo"].split(" + ")))
        pos = sp.build2(atoms, None, 1, row["transform"], row["gate"])
        gross = se.portfolio_returns(pos, "test")
        held = se._held(pos, s, e)
        first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, N), pos.dtype)
        traded = (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n_tick

        rec = {"combo": row["combo"], "transform": row["transform"],
               "train_sharpe": se.avg(pos, "train"), "test_sharpe": se.avg(pos, "test"),
               "train_a_sharpe": se.avg(pos, "train_a"), "train_b_sharpe": se.avg(pos, "train_b"),
               "test_cagr": ev.cagr(pos, "test"), "train_cagr": ev.cagr(pos, "train"),
               "exposure_test": se.exposure(pos, "test"),
               "turnover_yr": float(traded.mean() * 252),
               "z_net_5bps": row["z_net"], "z_lag2": row["z_lag2"],
               "breakeven_bps": row["breakeven_bps"]}
        for c in COST_LEVELS:
            rec[f"sharpe_{c}bps"] = sharpe_of(gross - traded * c / 1e4)
        rows.append(rec)

    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)
    pd.set_option("display.width", 280)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 40)

    print(f"{len(df)} combos clear every check (robust, z > {Z_BAR} both as traded "
          f"and with an extra bar of lag)\n")
    print(f"Buy & hold:  train Sharpe {bh['train']:.4f}   test Sharpe {bh['test']:.4f}   "
          f"test CAGR {ev.baseline['test']:.2%}\n")
    print(df[["combo", "transform", "train_sharpe", "test_sharpe", "train_a_sharpe",
              "train_b_sharpe", "test_cagr", "exposure_test", "turnover_yr",
              "z_net_5bps", "z_lag2", "breakeven_bps"]].to_string(
        index=False, float_format=lambda x: f"{x:.3f}"))

    print(f"\nPortfolio Sharpe vs round-trip cost (B&H = {sharpe_of(b):.4f}):")
    cols = ["combo", "transform"] + [f"sharpe_{c}bps" for c in COST_LEVELS]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
