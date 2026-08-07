"""Backtest 2- and 3-way combinations of the top individual indicators, and
rank them alongside the single-indicator leaderboard from indicator_backtest.py.

Takes the top TOP_N indicators from data/indicator_backtest_results.csv (by
SELECT_METRIC), forms every 2- and 3-indicator combination from that set, and
for each combo sums the member indicators' -1/0/1 position series and takes
the sign (ties -> flat) to get a single combo position per ticker. Backtested
the same way as a single indicator (see indicator_backtest._ticker_stats):
$10,000 notional per ticker, position shifted one bar before multiplying by
returns, returns floored at -0.999.

C(20,2)+C(20,3) = 1330 combos x ~500 tickers is too many individual pandas
backtests to run one at a time, so per ticker all combos are evaluated at
once via a (n_combos, n_top) membership matrix and vectorized numpy ops,
rather than looping over combos in Python.
"""

import itertools
import time
import warnings

import numpy as np
import pandas as pd
from tqdm import tqdm

from data_loader import load_universe
from indicator_backtest import (
    CAPITAL_PER_TICKER,
    MIN_ROWS,
    MIN_SHARPE_COVERAGE,
    NEEDS_BENCHMARK,
    TRADING_DAYS_PER_YEAR,
    _load_benchmark_close,
)
from sp500_tickers import get_sp500_tickers
from talib_signals import GENERIC_FALLBACK_FUNCTIONS, generate_position

RESULTS_CSV = "../data/indicator_backtest_results.csv"
COMBO_OUT_CSV = "../data/combo_backtest_results.csv"
MERGED_OUT_CSV = "../data/leaderboard_with_combos.csv"
TOP_N = 20
COMBO_SIZES = (2, 3)
SELECT_METRIC = "avg_excess_sharpe"
# "majority_vote": position = whichever of long/short a strict majority of
# members agree on, else flat (a 2-member combo where the two disagree, or a
# 3-member combo split 1/1/1, goes flat).
# "sum_sign": sum the members' -1/0/1 positions and take the sign of the sum.
COMBINE_METHOD = "majority_vote"


def load_top_indicators(results_path: str = RESULTS_CSV, top_n: int = TOP_N,
                         metric: str = SELECT_METRIC) -> list[str]:
    results = pd.read_csv(results_path, index_col="indicator")
    # BUYHOLD is the benchmark, not a candidate to combine with.
    if "is_baseline" in results.columns:
        results = results[~results["is_baseline"]]
    return results.sort_values(metric, ascending=False).head(top_n).index.tolist()


def build_combo_list(names: list[str], sizes: tuple[int, ...] = COMBO_SIZES) -> list[tuple[str, ...]]:
    combos = []
    for size in sizes:
        combos.extend(itertools.combinations(names, size))
    return combos


def compute_combo_stats(top_indicators: list[str], combos: list[tuple[str, ...]],
                         tickers: list[str] | None = None,
                         combine_method: str = COMBINE_METHOD) -> pd.DataFrame:
    if combine_method not in ("majority_vote", "sum_sign"):
        raise ValueError(f"Unknown combine_method: {combine_method!r}")

    data = load_universe(tickers or get_sp500_tickers())
    benchmark_close = _load_benchmark_close() if NEEDS_BENCHMARK & set(top_indicators) else None

    index_of = {name: i for i, name in enumerate(top_indicators)}
    n_top = len(top_indicators)
    n_combos = len(combos)
    combo_matrix = np.zeros((n_combos, n_top))
    for c, combo in enumerate(combos):
        for name in combo:
            combo_matrix[c, index_of[name]] = 1.0
    # Majority threshold per combo: >1 member of 2, >1.5 (i.e. >=2) of 3.
    majority_threshold = combo_matrix.sum(axis=1, keepdims=True) / 2

    valid_tickers = [(ticker, df) for ticker, df in data.items() if len(df) >= MIN_ROWS]
    n_tickers = len(valid_tickers)

    # Per-ticker buy-and-hold reference, so combos can be scored as excess over
    # the baseline the same way indicator_backtest.py scores single indicators.
    bh_total_return = np.full(n_tickers, np.nan)
    bh_cagr = np.full(n_tickers, np.nan)
    bh_sharpe = np.full(n_tickers, np.nan)

    total_return_mat = np.full((n_combos, n_tickers), np.nan)
    pnl_mat = np.full((n_combos, n_tickers), np.nan)
    cagr_mat = np.full((n_combos, n_tickers), np.nan)
    sharpe_mat = np.full((n_combos, n_tickers), np.nan)
    drawdown_mat = np.full((n_combos, n_tickers), np.nan)
    exposure_mat = np.full((n_combos, n_tickers), np.nan)

    for j, (ticker, df) in enumerate(tqdm(valid_tickers, desc="Backtesting combos per ticker")):
        daily_return = df["Close"].pct_change().to_numpy()
        n_days = len(df)

        # Buy-and-hold: long from bar 1 onward, so its return stream is just
        # the (floored) daily returns. Same math as every combo below.
        bh_ret = np.clip(daily_return[1:], -0.999, None)
        bh_equity = np.cumprod(1 + bh_ret)
        bh_years = len(bh_ret) / TRADING_DAYS_PER_YEAR
        bh_total_return[j] = bh_equity[-1] - 1
        bh_cagr[j] = bh_equity[-1] ** (1 / bh_years) - 1
        # ddof=1 to match pandas' Series.std() default, which is what
        # indicator_backtest._ticker_stats uses - otherwise combo Sharpes are
        # computed on a different basis than the single-indicator Sharpes they
        # get ranked against.
        bh_std = bh_ret.std(ddof=1)
        bh_sharpe[j] = (bh_ret.mean() / bh_std * np.sqrt(TRADING_DAYS_PER_YEAR)) if bh_std > 0 else np.nan

        positions = np.zeros((n_days, n_top))
        succeeded = np.zeros(n_top, dtype=bool)
        for i, name in enumerate(top_indicators):
            try:
                kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
                positions[:, i] = generate_position(name, df, **kwargs).to_numpy()
                succeeded[i] = True
            except Exception:
                pass

        # A combo is only usable on this ticker if every one of its members
        # produced a position here - otherwise it'd silently combine fewer
        # than its stated number of indicators.
        usable = (combo_matrix @ (~succeeded)) == 0
        if not usable.any():
            continue

        if combine_method == "majority_vote":
            # Position = whichever of {long, short} a strict majority of
            # members agree on; anything short of that (including a member
            # sitting flat) is a tie and the combo goes flat.
            count_long = combo_matrix @ (positions == 1).astype(float).T
            count_short = combo_matrix @ (positions == -1).astype(float).T
            combo_position = np.where(
                count_long > majority_threshold, 1.0,
                np.where(count_short > majority_threshold, -1.0, 0.0),
            )
        else:
            combo_position = np.sign(combo_matrix @ positions.T)  # (n_combos, n_days)
        shifted = np.zeros_like(combo_position)
        shifted[:, 1:] = combo_position[:, :-1]

        strat_return = shifted[:, 1:] * daily_return[1:][np.newaxis, :]
        strat_return = np.clip(strat_return, -0.999, None)

        equity = np.cumprod(1 + strat_return, axis=1)
        total_return = equity[:, -1] - 1
        n_years = strat_return.shape[1] / TRADING_DAYS_PER_YEAR
        cagr = equity[:, -1] ** (1 / n_years) - 1
        mean = strat_return.mean(axis=1)
        std = strat_return.std(axis=1, ddof=1)  # ddof=1: match pandas, see bh_std above
        with np.errstate(invalid="ignore", divide="ignore"):
            sharpe = np.where(std > 0, mean / std * np.sqrt(TRADING_DAYS_PER_YEAR), np.nan)
        running_max = np.maximum.accumulate(equity, axis=1)
        max_drawdown = (equity / running_max - 1).min(axis=1)

        total_return_mat[:, j] = np.where(usable, total_return, np.nan)
        pnl_mat[:, j] = np.where(usable, CAPITAL_PER_TICKER * total_return, np.nan)
        cagr_mat[:, j] = np.where(usable, cagr, np.nan)
        sharpe_mat[:, j] = np.where(usable, sharpe, np.nan)
        drawdown_mat[:, j] = np.where(usable, max_drawdown, np.nan)
        exposure_mat[:, j] = np.where(usable, (combo_position != 0).mean(axis=1), np.nan)

    n_valid = np.sum(~np.isnan(total_return_mat), axis=1)
    n_sharpe = np.sum(~np.isnan(sharpe_mat), axis=1)
    # An all-NaN row is the expected shape of a combo that never traded on any
    # ticker; it's flagged via n_sharpe/rankable below, so nanmean's warning
    # about it is just noise.
    warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
    with np.errstate(invalid="ignore"):
        total_pnl_dollars = np.nansum(pnl_mat, axis=1)
        total_pnl_dollars[n_valid == 0] = 0.0
        win_rate = np.full(n_combos, np.nan)
        has_data = n_valid > 0
        win_rate[has_data] = np.sum(total_return_mat[has_data] > 0, axis=1) / n_valid[has_data]

        # Excess over each ticker's own buy-and-hold, then averaged - matches
        # indicator_backtest.py so combo rows and single-indicator rows are
        # directly comparable in the merged leaderboard.
        excess_total_return_mat = total_return_mat - bh_total_return[np.newaxis, :]
        excess_cagr_mat = cagr_mat - bh_cagr[np.newaxis, :]
        excess_sharpe_mat = sharpe_mat - bh_sharpe[np.newaxis, :]
        beat_bh = total_return_mat > bh_total_return[np.newaxis, :]
        beat_buyhold_rate = np.full(n_combos, np.nan)
        beat_buyhold_rate[has_data] = np.sum(
            beat_bh[has_data] & ~np.isnan(total_return_mat[has_data]), axis=1
        ) / n_valid[has_data]

        result = pd.DataFrame({
            "indicator": ["+".join(c) for c in combos],
            "combo_size": [len(c) for c in combos],
            "n_tickers": n_valid,
            "total_pnl_dollars": total_pnl_dollars,
            "avg_pnl_dollars": np.nanmean(pnl_mat, axis=1),
            "avg_total_return": np.nanmean(total_return_mat, axis=1),
            "median_total_return": np.nanmedian(total_return_mat, axis=1),
            "avg_cagr": np.nanmean(cagr_mat, axis=1),
            "avg_sharpe": np.nanmean(sharpe_mat, axis=1),
            "avg_max_drawdown": np.nanmean(drawdown_mat, axis=1),
            "win_rate": win_rate,
            "avg_excess_total_return": np.nanmean(excess_total_return_mat, axis=1),
            "avg_excess_cagr": np.nanmean(excess_cagr_mat, axis=1),
            "avg_excess_sharpe": np.nanmean(excess_sharpe_mat, axis=1),
            "beat_buyhold_rate": beat_buyhold_rate,
            # See indicator_backtest.py: Sharpe is NaN wherever a combo never
            # traded, so its average can rest on far fewer tickers than
            # n_tickers implies. Surface the real denominator and exposure.
            "n_sharpe": n_sharpe,
            "avg_exposure": np.nanmean(exposure_mat, axis=1),
            "rankable": n_sharpe >= MIN_SHARPE_COVERAGE * np.maximum(n_valid, 1),
            "generic_fallback": [any(m in GENERIC_FALLBACK_FUNCTIONS for m in c) for c in combos],
            "is_baseline": False,
        }).set_index("indicator")
    return result


if __name__ == "__main__":
    start = time.time()

    top_indicators = load_top_indicators()
    combos = build_combo_list(top_indicators)
    print(f"Top {TOP_N} indicators by {SELECT_METRIC}: {top_indicators}")
    print(f"Backtesting {len(combos)} combos (sizes {COMBO_SIZES})...")

    combo_results = compute_combo_stats(top_indicators, combos)
    combo_results = combo_results.sort_values("avg_excess_sharpe", ascending=False)
    combo_results.to_csv(COMBO_OUT_CSV)

    individual_results = pd.read_csv(RESULTS_CSV, index_col="indicator")
    individual_results["combo_size"] = 1
    merged = pd.concat([individual_results, combo_results], axis=0)
    # Same convention as indicator_backtest.py: rank on excess over
    # buy-and-hold, with rules too rarely in the market to score meaningfully
    # pushed below the ranking and the baseline parked at the bottom.
    real = merged[~merged["is_baseline"]]
    ranked = real[real["rankable"]].sort_values("avg_excess_sharpe", ascending=False)
    unrankable = real[~real["rankable"]].sort_values("avg_excess_sharpe", ascending=False)
    merged = pd.concat([ranked, unrankable, merged[merged["is_baseline"]]])
    merged.to_csv(MERGED_OUT_CSV)

    elapsed = time.time() - start
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 25)
    print(f"\nBacktested {len(combos)} indicator combos in {elapsed:.1f}s")
    print(f"Combo-only results written to {COMBO_OUT_CSV}")
    print(f"Merged leaderboard (individual + combos) written to {MERGED_OUT_CSV}\n")

    cols = ["combo_size", "n_sharpe", "avg_exposure", "avg_excess_sharpe",
            "avg_excess_cagr", "beat_buyhold_rate", "avg_cagr"]
    n_excluded = len(unrankable)
    if n_excluded:
        print(f"{n_excluded} rules excluded from ranking (valid Sharpe on < "
              f"{MIN_SHARPE_COVERAGE:.0%} of tickers - too rarely in the market to score)")
    best_single = ranked[ranked["combo_size"] == 1].iloc[0]
    beating = ranked[ranked["avg_excess_sharpe"] > best_single["avg_excess_sharpe"]]
    print(f"Best single indicator: {ranked[ranked['combo_size'] == 1].index[0]} "
          f"(avg_excess_sharpe {best_single['avg_excess_sharpe']:.4f})")
    print(f"{len(beating)} combos beat it on avg_excess_sharpe\n")
    print("Top 20 by excess Sharpe over buy-and-hold (individual + combos):")
    print(ranked.head(20)[cols])
    print("\nBuy-and-hold baseline (reference, not ranked):")
    print(merged[merged["is_baseline"]][cols])
