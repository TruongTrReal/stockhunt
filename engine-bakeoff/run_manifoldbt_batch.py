"""manifoldbt's batch path — the one it is actually designed for.

``mbt.run()`` reloads and re-aligns the bars on every call, so a 200-run loop
pays that 200 times. ``run_batch_lite`` loads once and evaluates every strategy
against the same data on separate threads, which is the shape of the real
workload here (one ticker, many indicators). This script measures that path and
checks it still lands on the same equity, so the speed number is only claimed
where the accuracy holds.

Community tier caps a batch at 500 strategies per call; 5 rules per ticker is
well inside it.
"""

from __future__ import annotations

import time

import pandas as pd

import manifoldbt as mbt
from manifoldbt import BacktestConfig, ExecutionConfig, Strategy, col, exo
from manifoldbt.helpers import ExecutionPrice, Interval, Slippage

from common import INITIAL_CAPITAL, RESULTS_DIR, RULES, UNIVERSE
from run_manifoldbt import ONE_DAY_NS, build_store


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    t0 = time.perf_counter()
    store, bars_by_ticker, symbol_ids = build_store()
    setup_seconds = time.perf_counter() - t0

    rows = []
    engine_seconds = 0.0

    for ticker in UNIVERSE:
        bars = bars_by_ticker[ticker]
        ts0 = pd.Timestamp(bars.index[0], tz="UTC").value
        ts1 = pd.Timestamp(bars.index[-1], tz="UTC").value + ONE_DAY_NS

        strategies = [
            Strategy.create(f"{ticker}_{rule}")
            .signal("target", exo(f"sig_{ticker}_{rule}"))
            .size(col("target"))
            for rule in RULES
        ]
        cfg = BacktestConfig(
            universe=[symbol_ids[ticker]],
            time_range_start=int(ts0),
            time_range_end=int(ts1),
            initial_capital=INITIAL_CAPITAL,
            bar_interval=Interval.hours(1),
            exo_data=[f"sig_{ticker}_{rule}" for rule in RULES],
            slippage=Slippage.none(),
            execution=ExecutionConfig(
                signal_delay=0,
                execution_price=ExecutionPrice.AT_CLOSE,
                allow_short=False,
                allow_fractional=True,
                position_sizing_mode="FractionOfEquity",
            ),
        )

        t0 = time.perf_counter()
        results = mbt.run_batch_lite(strategies, cfg, store)
        engine_seconds += time.perf_counter() - t0

        for rule, res in zip(RULES, results):
            rows.append({
                "engine": "manifoldbt_batch",
                "convention": "at_close",
                "ticker": ticker,
                "rule": rule,
                "n_bars": len(bars),
                "final_equity": float(res.final_equity),
                "total_return": float(res.final_equity) / INITIAL_CAPITAL - 1.0,
                "n_trades": int(res.trade_count),
            })

    out = pd.DataFrame(rows)
    out.to_csv(RESULTS_DIR / "manifoldbt_batch.csv", index=False)
    pd.DataFrame([{
        "engine": "manifoldbt_batch",
        "n_runs": len(out),
        "setup_seconds": setup_seconds,
        "engine_seconds": engine_seconds,
        "total_seconds": setup_seconds + engine_seconds,
    }]).to_csv(RESULTS_DIR / "timing_manifoldbt_batch.csv", index=False)

    print(f"manifoldbt run_batch_lite: {len(out)} runs in {engine_seconds:.2f}s "
          f"({engine_seconds / len(out) * 1000:.2f} ms/run), "
          f"setup {setup_seconds:.2f}s")


if __name__ == "__main__":
    main()
