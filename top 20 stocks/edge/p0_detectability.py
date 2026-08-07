"""Plan 0 - what Sharpe improvement is this sample even capable of detecting?

Run before any search. If the detectable floor is 0.35 and a candidate shows +0.08,
that candidate is noise regardless of how good the story is. This is the number the
project has been missing: every previous "winner" was reported without one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, TRADING_DAYS, block_bootstrap_se, deflated_threshold,
                    load_daily, log_trial, sharpe, split, summarise)


def main() -> None:
    data = load_daily()
    rets = pd.DataFrame({t: df["Close"].pct_change() for t, df in data.items()}).dropna(how="all")
    train, holdout = split(rets)

    ew = train.mean(axis=1)  # equal-weight, rebalanced daily = the benchmark to beat
    print(f"TRAIN {train.index[0].date()} -> {train.index[-1].date()}  "
          f"({len(train)} days, {train.shape[1]} tickers)")
    print(f"HOLDOUT (sealed) {holdout.index[0].date()} -> {holdout.index[-1].date()} ({len(holdout)} days)\n")

    b = summarise(ew)
    print(f"Equal-weight B&H on TRAIN: Sharpe {b['sharpe']:.3f}  CAGR {b['cagr']:.2%}  "
          f"vol {b['vol']:.2%}  maxDD {b['max_dd']:.1%}")

    se_sr = block_bootstrap_se(ew, sharpe, block=21, n_boot=3000)
    print(f"\nBlock-bootstrap SE of a Sharpe estimate on this sample: {se_sr:.3f}")

    # A candidate correlated rho with the benchmark has SE(dSharpe) ~ SE(SR)*sqrt(2(1-rho)).
    # High correlation is GOOD for detection: a tilt overlay that tracks the benchmark
    # closely needs a much smaller true edge to prove itself than a standalone strategy.
    print(f"\n{'rho to B&H':>12} {'SE(dSharpe)':>12} | minimum detectable excess Sharpe")
    print(f"{'':>12} {'':>12} | {'1 trial':>9} {'100':>9} {'10k':>9} {'170k':>9}")
    rows = {}
    for rho in [0.0, 0.5, 0.8, 0.9, 0.95, 0.99]:
        se_d = se_sr * np.sqrt(2 * (1 - rho))
        thr = {m: deflated_threshold(m, se_d) for m in (1, 100, 10_000, 170_000)}
        rows[rho] = thr
        print(f"{rho:>12.2f} {se_d:>12.3f} | {thr[1]:>9.3f} {thr[100]:>9.3f} "
              f"{thr[10_000]:>9.3f} {thr[170_000]:>9.3f}")

    print("\nCost drag on the benchmark itself, for reference:")
    for bps in COST_BPS_GRID:
        print(f"  {bps:>4.0f}bps x 1 round-trip/day = {2 * bps / 1e4 * TRADING_DAYS:>6.2%}/yr")

    log_trial("P0", "calibration - detectable effect size", "TRAIN",
              "floor established", {"se_sharpe": se_sr, "bh_sharpe": b["sharpe"],
                                    "bh_cagr": b["cagr"], "thr_rho0.9_100trials": rows[0.9][100]},
              "not a hypothesis test; sets the bar for every later plan")


if __name__ == "__main__":
    main()
