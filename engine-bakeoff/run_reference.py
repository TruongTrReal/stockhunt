"""Analytic reference: the ground truth both engines are scored against.

Runs every (ticker, rule) pair under both execution conventions with a plain
share-level simulation. Also times the pure signal+simulation cost, which is the
floor any engine has to beat — it is what the existing project already does.
"""

from __future__ import annotations

import time

import pandas as pd

from common import (
    CONVENTIONS, INITIAL_CAPITAL, RESULTS_DIR, RULES, UNIVERSE,
    execution_arrays, load_bars, positions_for, simulate,
)


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    rows = []
    signal_seconds = 0.0
    sim_seconds = 0.0

    for ticker in UNIVERSE:
        df = load_bars(ticker)

        t0 = time.perf_counter()
        pos = positions_for(ticker, df)
        signal_seconds += time.perf_counter() - t0

        bars = df.loc[pos.index]
        close = bars["Close"].to_numpy()

        for convention in CONVENTIONS:
            for rule in RULES:
                exec_target, exec_price = execution_arrays(
                    bars, pos[rule].to_numpy(), convention)
                t0 = time.perf_counter()
                res = simulate(close, exec_price, exec_target, INITIAL_CAPITAL)
                sim_seconds += time.perf_counter() - t0
                rows.append({
                    "engine": "reference",
                    "convention": convention,
                    "ticker": ticker,
                    "rule": rule,
                    "n_bars": len(bars),
                    "final_equity": res["final_equity"],
                    "total_return": res["total_return"],
                    "n_trades": res["n_trades"],
                })

    out = pd.DataFrame(rows)
    out.to_csv(RESULTS_DIR / "reference.csv", index=False)

    total = signal_seconds + sim_seconds
    pd.DataFrame([{
        "engine": "reference",
        "n_runs": len(out),
        "signal_seconds": signal_seconds,
        "engine_seconds": sim_seconds,
        "total_seconds": total,
    }]).to_csv(RESULTS_DIR / "timing_reference.csv", index=False)

    print(f"reference: {len(out)} runs "
          f"({len(UNIVERSE)} tickers x {len(RULES)} rules x 2 conventions)")
    print(f"  TA-Lib signal time : {signal_seconds:.3f}s")
    print(f"  simulation time    : {sim_seconds:.3f}s")
    print(f"  total              : {total:.3f}s")


if __name__ == "__main__":
    main()
