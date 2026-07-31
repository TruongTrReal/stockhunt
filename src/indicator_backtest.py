"""Backtest all 161 TA-Lib indicators, each as a standalone strategy, across
the whole cached S&P 500 universe, and rank indicators by aggregate PnL.

Each indicator gets one position rule (see talib_signals.py — ~120 have a
textbook rule, ~40 have no natural direction and fall back to a generic "above
its own trailing SMA" rule, flagged via GENERIC_FALLBACK_FUNCTIONS), applied to
every ticker independently with a fixed $10,000 notional allocation. "PnL" is
the sum of dollar gains across all tickers for that indicator's strategy over
the full cached history (2015-01-01 to present). This is a comparison of
simple, single-indicator rules — not a tuned or risk-managed strategy.
"""

import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from data_loader import load_universe, update_universe, DEFAULT_START
from sp500_tickers import get_sp500_tickers
from talib_signals import (
    BENCHMARK_TICKER,
    BETA_NAME,
    CORREL_NAME,
    GENERIC_FALLBACK_FUNCTIONS,
    generate_position,
    get_all_indicator_names,
)

TRADING_DAYS_PER_YEAR = 252
CAPITAL_PER_TICKER = 10_000
MIN_ROWS = 100
NEEDS_BENCHMARK = {BETA_NAME, CORREL_NAME}
# Must match build_report_data.BASELINE_NAME. Defined locally rather than
# imported so the backtest layer doesn't depend on the report-builder layer.
BASELINE_NAME = "BUYHOLD"
# A rule must produce a valid (non-NaN) Sharpe on at least this fraction of the
# tickers it ran on to be eligible for ranking. Rules that are almost always
# flat otherwise win on a handful of tickers' worth of noise.
MIN_SHARPE_COVERAGE = 0.8


def _ticker_stats(df: pd.DataFrame, position: pd.Series) -> dict | None:
    daily_return = df["Close"].pct_change()
    strategy_return = position.shift(1) * daily_return
    strategy_return = strategy_return.dropna()
    if strategy_return.empty:
        return None
    # A short position can lose more than 100% in one bar (price more than
    # doubles). Floor at -100% so compounded equity can't go negative and
    # then spuriously flip back positive on the next multiply.
    strategy_return = strategy_return.clip(lower=-0.999)

    equity_curve = (1 + strategy_return).cumprod()
    total_return = equity_curve.iloc[-1] - 1
    pnl_dollars = CAPITAL_PER_TICKER * total_return

    n_years = len(strategy_return) / TRADING_DAYS_PER_YEAR
    cagr = equity_curve.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else np.nan
    std = strategy_return.std()
    sharpe = (strategy_return.mean() / std) * np.sqrt(TRADING_DAYS_PER_YEAR) if std else np.nan
    running_max = equity_curve.cummax()
    max_drawdown = (equity_curve / running_max - 1).min()

    return {
        "total_return": total_return,
        "pnl_dollars": pnl_dollars,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        # Fraction of bars actually holding a position. A rule that is flat
        # almost always produces a near-zero return stream whose Sharpe is
        # noise (or NaN when std is exactly 0), so this has to be visible.
        "exposure": float((position != 0).mean()),
    }


def _position_for(name: str, df: pd.DataFrame, benchmark_close: pd.Series | None) -> pd.Series:
    if name == BASELINE_NAME:
        return pd.Series(1.0, index=df.index)  # buy on day 1, hold forever, never trade again
    kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
    return generate_position(name, df, **kwargs)


def _load_benchmark_close() -> pd.Series:
    """SPY close series, used only by BETA/CORREL. Downloads+caches if missing."""
    bench = load_universe([BENCHMARK_TICKER])
    if BENCHMARK_TICKER not in bench:
        update_universe(tickers=[BENCHMARK_TICKER], start=DEFAULT_START)
        bench = load_universe([BENCHMARK_TICKER])
    return bench[BENCHMARK_TICKER]["Close"]


def run_all_indicators(tickers: list[str] | None = None, indicator_names: list[str] | None = None) -> pd.DataFrame:
    # Default to the S&P 500 constituent list, not "every parquet in the cache
    # dir" - the cache also holds the SPY benchmark series used by BETA/CORREL,
    # which must never itself be scored as one of the backtested stocks.
    data = load_universe(tickers or get_sp500_tickers())
    indicator_names = indicator_names or get_all_indicator_names()
    # BUYHOLD is scored like any other rule so its row can be compared directly,
    # but it's the benchmark every indicator is measured against, not a ranked
    # competitor - it's pulled out of the ranking at the end.
    if BASELINE_NAME not in indicator_names:
        indicator_names = indicator_names + [BASELINE_NAME]
    benchmark_close = _load_benchmark_close() if NEEDS_BENCHMARK & set(indicator_names) else None

    per_indicator_rows: dict[str, list[dict]] = {name: [] for name in indicator_names}
    skipped_tickers = 0

    for ticker, df in tqdm(data.items(), desc="Backtesting tickers"):
        if len(df) < MIN_ROWS:
            skipped_tickers += 1
            continue
        # Per-ticker buy-and-hold reference. Without it an indicator that is
        # simply long most of the time scores well purely because the market
        # rose, which says nothing about the rule itself.
        baseline_stats = _ticker_stats(df, _position_for(BASELINE_NAME, df, benchmark_close))
        if baseline_stats is None:
            skipped_tickers += 1
            continue
        for name in indicator_names:
            try:
                position = _position_for(name, df, benchmark_close)
                stats = _ticker_stats(df, position)
            except Exception:
                stats = None
            if stats is not None:
                per_indicator_rows[name].append({
                    **stats,
                    "excess_total_return": stats["total_return"] - baseline_stats["total_return"],
                    "excess_cagr": stats["cagr"] - baseline_stats["cagr"],
                    "excess_sharpe": stats["sharpe"] - baseline_stats["sharpe"],
                    "beat_buyhold": stats["total_return"] > baseline_stats["total_return"],
                })

    summary_rows = []
    for name, rows in per_indicator_rows.items():
        if not rows:
            summary_rows.append({
                "indicator": name, "n_tickers": 0, "total_pnl_dollars": 0.0, "avg_pnl_dollars": np.nan,
                "avg_total_return": np.nan, "median_total_return": np.nan, "avg_cagr": np.nan,
                "avg_sharpe": np.nan, "avg_max_drawdown": np.nan, "win_rate": np.nan,
                "avg_excess_total_return": np.nan, "avg_excess_cagr": np.nan,
                "avg_excess_sharpe": np.nan, "beat_buyhold_rate": np.nan,
                "n_sharpe": 0, "avg_exposure": np.nan, "rankable": False,
                "generic_fallback": name in GENERIC_FALLBACK_FUNCTIONS,
                "is_baseline": name == BASELINE_NAME,
            })
            continue
        stats_df = pd.DataFrame(rows)
        summary_rows.append({
            "indicator": name,
            "n_tickers": len(stats_df),
            "total_pnl_dollars": stats_df["pnl_dollars"].sum(),
            "avg_pnl_dollars": stats_df["pnl_dollars"].mean(),
            "avg_total_return": stats_df["total_return"].mean(),
            "median_total_return": stats_df["total_return"].median(),
            "avg_cagr": stats_df["cagr"].mean(),
            "avg_sharpe": stats_df["sharpe"].mean(),
            "avg_max_drawdown": stats_df["max_drawdown"].mean(),
            "win_rate": (stats_df["total_return"] > 0).mean(),
            "avg_excess_total_return": stats_df["excess_total_return"].mean(),
            "avg_excess_cagr": stats_df["excess_cagr"].mean(),
            "avg_excess_sharpe": stats_df["excess_sharpe"].mean(),
            "beat_buyhold_rate": stats_df["beat_buyhold"].mean(),
            # Sharpe is NaN on tickers where the rule never traded, and pandas'
            # mean() skips those - so the Sharpe columns can be averaged over
            # far fewer tickers than n_tickers suggests. Record the real
            # denominator rather than letting it hide.
            "n_sharpe": int(stats_df["sharpe"].notna().sum()),
            "avg_exposure": stats_df["exposure"].mean(),
            "rankable": bool(stats_df["sharpe"].notna().sum() >= MIN_SHARPE_COVERAGE * len(stats_df)),
            "generic_fallback": name in GENERIC_FALLBACK_FUNCTIONS,
            "is_baseline": name == BASELINE_NAME,
        })

    summary = pd.DataFrame(summary_rows).set_index("indicator")
    # Rank on excess-over-buy-and-hold, not raw Sharpe: raw Sharpe in a rising
    # market mostly measures how long-biased a rule is. The baseline sorts to
    # the bottom by construction (its excess is exactly 0) but is kept in the
    # output as the reference row, so it's appended after ranking rather than
    # competing in it.
    real = summary[~summary["is_baseline"]]
    ranked = real[real["rankable"]].sort_values("avg_excess_sharpe", ascending=False)
    unrankable = real[~real["rankable"]].sort_values("avg_excess_sharpe", ascending=False)
    summary = pd.concat([ranked, unrankable, summary[summary["is_baseline"]]])
    print(f"Skipped {skipped_tickers} tickers with < {MIN_ROWS} rows of history")
    zero_row_indicators = summary.index[summary["n_tickers"] == 0].tolist()
    if zero_row_indicators:
        print(f"{len(zero_row_indicators)} indicators produced no valid signal on any ticker: {zero_row_indicators}")
    return summary


if __name__ == "__main__":
    start = time.time()
    summary = run_all_indicators()
    elapsed = time.time() - start

    out_path = "../data/indicator_backtest_results.csv"
    summary.to_csv(out_path)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 25)
    n_fallback = int(summary["generic_fallback"].sum())
    print(f"\nRan all {len(summary)} TA-Lib indicators across the universe in {elapsed:.1f}s")
    print(f"{n_fallback} of these use a generic 'value vs own SMA' fallback rule, not a textbook rule "
          f"(see talib_signals.GENERIC_FALLBACK_FUNCTIONS) - interpret with caution")
    print(f"Full results written to {out_path}\n")

    real = summary[~summary["is_baseline"]]
    n_beat = int((real["avg_excess_sharpe"] > 0).sum())
    print(f"{n_beat} of {len(real)} indicators beat buy-and-hold on average per-ticker Sharpe")

    cols = ["n_tickers", "avg_excess_sharpe", "avg_excess_cagr", "beat_buyhold_rate",
            "avg_sharpe", "avg_cagr", "generic_fallback"]
    print("\nTop 10 indicators by excess Sharpe over buy-and-hold:")
    print(summary.head(10)[cols])
    print("\nBuy-and-hold baseline (reference, not ranked):")
    print(summary[summary["is_baseline"]][cols])
