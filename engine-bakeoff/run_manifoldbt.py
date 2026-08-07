"""manifoldbt leg of the bake-off.

manifoldbt has no TA-Lib binding — its strategies are built from its own Rust
expression DSL (~40 indicators). So the only way to run a TA-Lib rule through it
is to compute the rule in Python and inject the resulting target-position series
as an *exogenous* column, which is what this script does. That is a real
ergonomic cost, not a trick: it means the engine can never see the indicator, so
nothing that depends on the indicator's value (parameter sweeps, stop levels
derived from ATR, ...) can stay inside the engine.

Two engine quirks are worked around here rather than hidden, because both were
discovered by this harness and both change results:

1. ``interval="1d"`` data is unreadable — every backtest over a store imported
   at 1d returns "empty bar dataset". Daily bars are therefore imported under
   the ``"1h"`` label. Bars are processed in sequence, so equity and trades are
   unaffected; only the engine's own time-annualised metrics would be wrong, and
   this harness recomputes those from the equity curve instead of reading them.

2. Execution price and signal delay are independent knobs, and the ``NEXT_BAR_*``
   names do NOT imply a delay: with the default ``signal_delay=0`` they execute
   on the signal bar itself. A true next-bar-open fill is
   ``signal_delay=1`` + ``AT_OPEN``; see ANALYSIS.md for what the open-priced
   modes do to position sizing.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

import manifoldbt as mbt
from manifoldbt import BacktestConfig, ExecutionConfig, Strategy, col, exo
from manifoldbt.helpers import ExecutionPrice, Interval, Slippage

from common import (
    CONVENTIONS, INITIAL_CAPITAL, RESULTS_DIR, RULES, UNIVERSE,
    load_bars, positions_for,
)

STORE_DIR = Path(__file__).resolve().parent / "_mbt_store"
ONE_DAY_NS = 86_400_000_000_000

# convention -> (signal_delay, execution_price). See the module docstring: the
# NEXT_BAR_* enum values do not delay anything on their own.
EXECUTION = {
    "at_close": (0, ExecutionPrice.AT_CLOSE),
    "next_open": (1, ExecutionPrice.AT_OPEN),
}


def build_store() -> tuple[object, dict, dict]:
    """Import every ticker's bars and every (ticker, rule) target series.

    Returns ``(store, bars_by_ticker, symbol_ids)``.
    """
    if STORE_DIR.exists():
        shutil.rmtree(STORE_DIR)
    STORE_DIR.mkdir(parents=True)

    store = None
    bars_by_ticker: dict[str, pd.DataFrame] = {}
    symbol_ids: dict[str, int] = {}

    for sid, ticker in enumerate(UNIVERSE, start=1):
        df = load_bars(ticker)
        pos = positions_for(ticker, df)
        bars = df.loc[pos.index]
        bars_by_ticker[ticker] = bars
        symbol_ids[ticker] = sid

        imp = bars.reset_index().rename(columns={
            "Date": "timestamp", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume"})
        imp["timestamp"] = pd.to_datetime(imp["timestamp"], utc=True)

        store = mbt.import_dataframe(
            imp, symbol=ticker, symbol_id=sid, interval="1h",
            data_root=str(STORE_DIR / "data"),
            metadata_db=str(STORE_DIR / "metadata.sqlite"),
            exchange="CACHE", asset_class="equity",
        )
        for rule in RULES:
            mbt.register_exo(
                f"sig_{ticker}_{rule}",
                pd.DataFrame({"timestamp": imp["timestamp"],
                              f"sig_{ticker}_{rule}": pos[rule].to_numpy()}),
                store=store, timeframe="1h",
            )
    return store, bars_by_ticker, symbol_ids


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    t0 = time.perf_counter()
    store, bars_by_ticker, symbol_ids = build_store()
    setup_seconds = time.perf_counter() - t0

    rows = []
    engine_seconds = 0.0

    for convention in CONVENTIONS:
        delay, exec_price = EXECUTION[convention]
        for ticker in UNIVERSE:
            bars = bars_by_ticker[ticker]
            ts0 = pd.Timestamp(bars.index[0], tz="UTC").value
            ts1 = pd.Timestamp(bars.index[-1], tz="UTC").value + ONE_DAY_NS
            for rule in RULES:
                name = f"sig_{ticker}_{rule}"
                strategy = (Strategy.create(f"{ticker}_{rule}")
                            .signal("target", exo(name))
                            .size(col("target")))
                cfg = BacktestConfig(
                    universe=[symbol_ids[ticker]],
                    time_range_start=int(ts0),
                    time_range_end=int(ts1),
                    initial_capital=INITIAL_CAPITAL,
                    bar_interval=Interval.hours(1),
                    exo_data=[name],
                    slippage=Slippage.none(),
                    execution=ExecutionConfig(
                        signal_delay=delay,
                        execution_price=exec_price,
                        allow_short=False,
                        allow_fractional=True,
                        position_sizing_mode="FractionOfEquity",
                    ),
                )
                t0 = time.perf_counter()
                res = mbt.run(strategy, cfg, store)
                engine_seconds += time.perf_counter() - t0

                equity = np.asarray(res.raw.equity_curve)
                rows.append({
                    "engine": "manifoldbt",
                    "convention": convention,
                    "ticker": ticker,
                    "rule": rule,
                    "n_bars": len(equity),
                    "final_equity": float(equity[-1]),
                    "total_return": float(equity[-1] / INITIAL_CAPITAL - 1.0),
                    "n_trades": int(res.raw.trade_count),
                })

    out = pd.DataFrame(rows)
    out.to_csv(RESULTS_DIR / "manifoldbt.csv", index=False)
    pd.DataFrame([{
        "engine": "manifoldbt",
        "n_runs": len(out),
        "setup_seconds": setup_seconds,
        "engine_seconds": engine_seconds,
        "total_seconds": setup_seconds + engine_seconds,
    }]).to_csv(RESULTS_DIR / "timing_manifoldbt.csv", index=False)

    print(f"\nmanifoldbt: {len(out)} runs")
    print(f"  data import + exo registration : {setup_seconds:.2f}s (one-time)")
    print(f"  engine time                    : {engine_seconds:.2f}s "
          f"({engine_seconds / len(out) * 1000:.1f} ms/run)")
    print(f"  total                          : {setup_seconds + engine_seconds:.2f}s")


if __name__ == "__main__":
    main()
