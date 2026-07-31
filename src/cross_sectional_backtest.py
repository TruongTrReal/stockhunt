"""Backtest every indicator as a market-neutral CROSS-SECTIONAL rule instead of
a per-ticker timing rule, and rank the results.

The per-ticker backtests (indicator_backtest.py) ask "when should I be long this
stock?" - and in a rising market that question is dominated by how long-biased
the rule is, which is why nothing there beats buy-and-hold. This script asks a
different question: "on any given day, which stocks does this indicator like
*more than the others*?" Market direction cancels out, so buy-and-hold is no
longer the thing to beat - a zero return is.

Construction, per indicator, per day:
  1. Take each ticker's -1/0/1 position signal (talib_signals.generate_position).
  2. Subtract that day's cross-sectional mean, so a signal that is bullish on
     everything nets to zero rather than to a big long. This is what strips out
     the long-bias.
  3. Scale so gross exposure (sum of |weight|) is 1. The book is dollar-neutral
     by construction: longs and shorts cancel.
  4. Hold those weights for one bar before earning returns, same no-look-ahead
     convention as the rest of the pipeline.

Turnover is reported because a cross-sectional book rebalances every day, so
transaction costs bite much harder here than in a per-ticker timing rule -
a headline Sharpe means little without knowing what it costs to run.
"""

import time
import warnings

import numpy as np
import pandas as pd
from tqdm import tqdm

from data_loader import load_universe
from indicator_backtest import (
    MIN_ROWS,
    NEEDS_BENCHMARK,
    TRADING_DAYS_PER_YEAR,
    _load_benchmark_close,
)
from sp500_tickers import get_sp500_tickers
from talib_signals import (
    BENCHMARK_TICKER,
    GENERIC_FALLBACK_FUNCTIONS,
    generate_position,
    get_all_indicator_names,
)

OUT_CSV = "../data/cross_sectional_results.csv"
# A day needs at least this many tickers with a live signal to form a
# meaningful cross-section; early history has only a handful listed.
MIN_TICKERS_PER_DAY = 30
# Round-trip cost charged on turnover, in basis points of traded notional.
COST_BPS = 5.0


def _build_panel(data: dict[str, pd.DataFrame], master_index: pd.DatetimeIndex):
    """(returns matrix, ticker list). Returns are computed per ticker *before*
    reindexing, so a gap in one ticker's history can't be mistaken for a
    multi-day price move."""
    tickers = sorted(data)
    returns = np.full((len(master_index), len(tickers)), np.nan)
    for i, ticker in enumerate(tickers):
        r = data[ticker]["Close"].pct_change()
        returns[:, i] = r.reindex(master_index).to_numpy()
    return returns, tickers


def _weights_from_signal(signal: np.ndarray) -> np.ndarray:
    """Cross-sectionally demean each row, then scale to unit gross exposure.
    NaN (ticker not trading that day) is excluded from the mean and gets zero
    weight rather than silently counting as a flat position."""
    valid = ~np.isnan(signal)
    n_valid = valid.sum(axis=1)

    with np.errstate(invalid="ignore"):
        row_mean = np.nanmean(np.where(valid, signal, np.nan), axis=1, keepdims=True)
    demeaned = np.where(valid, signal - row_mean, 0.0)

    gross = np.abs(demeaned).sum(axis=1, keepdims=True)
    # gross == 0 means the indicator said the same thing about every ticker
    # that day - no cross-sectional opinion, so no position.
    weights = np.divide(demeaned, gross, out=np.zeros_like(demeaned), where=gross > 0)
    weights[n_valid < MIN_TICKERS_PER_DAY, :] = 0.0
    return weights


def _portfolio_stats(weights: np.ndarray, returns: np.ndarray) -> dict | None:
    """Weights are formed on bar t and earn bar t+1's return."""
    held = weights[:-1, :]
    earned = returns[1:, :]
    contrib = np.where(np.isnan(earned), 0.0, held * np.nan_to_num(earned))
    port_return = contrib.sum(axis=1)

    active = np.abs(held).sum(axis=1) > 0
    if active.sum() < TRADING_DAYS_PER_YEAR:  # < ~1 year of live positions
        return None

    # Turnover: how much of the book is rewritten each day. Charged as a cost
    # below; also reported raw so a high-Sharpe/high-churn rule is visible.
    turnover = np.abs(np.diff(weights, axis=0)).sum(axis=1)
    avg_turnover = float(turnover[active].mean())

    port_return = np.clip(port_return, -0.999, None)
    net_return = np.clip(port_return - turnover * COST_BPS / 10_000, -0.999, None)

    def _curve_stats(r):
        equity = np.cumprod(1 + r)
        n_years = len(r) / TRADING_DAYS_PER_YEAR
        cagr = equity[-1] ** (1 / n_years) - 1 if equity[-1] > 0 else np.nan
        std = r.std(ddof=1)
        sharpe = (r.mean() / std * np.sqrt(TRADING_DAYS_PER_YEAR)) if std > 0 else np.nan
        max_dd = float((equity / np.maximum.accumulate(equity) - 1).min())
        return cagr, sharpe, max_dd, equity[-1] - 1

    cagr, sharpe, max_dd, total_return = _curve_stats(port_return)
    net_cagr, net_sharpe, _, _ = _curve_stats(net_return)

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "total_return": total_return,
        "max_drawdown": max_dd,
        "net_sharpe": net_sharpe,
        "net_cagr": net_cagr,
        "avg_turnover": avg_turnover,
        "n_days": int(active.sum()),
        "port_return": port_return,
    }


def run_cross_sectional(tickers: list[str] | None = None,
                        indicator_names: list[str] | None = None,
                        invert: bool = False) -> pd.DataFrame:
    """invert=True flips every signal's sign (long what the indicator dislikes).
    Gross Sharpe is exactly negated by this, but net-of-cost Sharpe is NOT:
    turnover costs subtract in both directions, so the inverted book has to be
    run properly rather than inferred by flipping the sign of the original."""
    data = load_universe(tickers or get_sp500_tickers())
    data = {t: df for t, df in data.items() if len(df) >= MIN_ROWS and t != BENCHMARK_TICKER}
    indicator_names = indicator_names or get_all_indicator_names()
    benchmark_close = _load_benchmark_close() if NEEDS_BENCHMARK & set(indicator_names) else None

    master_index = pd.DatetimeIndex(sorted(set().union(*[df.index for df in data.values()])))
    returns, ticker_list = _build_panel(data, master_index)
    print(f"Panel: {len(master_index)} days x {len(ticker_list)} tickers "
          f"({master_index.min():%Y-%m-%d} to {master_index.max():%Y-%m-%d})")

    # Market return = equal-weight average across the panel, for the beta check
    # that verifies these books really are market-neutral. The first bar (and
    # any all-missing day) is an empty slice by construction - zeroed below.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
        market_return = np.nanmean(returns, axis=1)
    market_return = np.nan_to_num(market_return)[1:]

    rows = []
    for name in tqdm(indicator_names, desc="Cross-sectional backtest"):
        signal = np.full((len(master_index), len(ticker_list)), np.nan, dtype=np.float32)
        n_ok = 0
        for i, ticker in enumerate(ticker_list):
            try:
                kwargs = {"benchmark_close": benchmark_close} if name in NEEDS_BENCHMARK else {}
                pos = generate_position(name, data[ticker], **kwargs)
            except Exception:
                continue
            signal[:, i] = pos.reindex(master_index).to_numpy()
            n_ok += 1
        if n_ok < MIN_TICKERS_PER_DAY:
            continue

        panel = signal.astype(np.float64)
        if invert:
            panel = -panel
        stats = _portfolio_stats(_weights_from_signal(panel), returns)
        if stats is None:
            continue

        port = stats.pop("port_return")
        mvar = market_return.var()
        beta = float(np.cov(port, market_return)[0, 1] / mvar) if mvar > 0 else np.nan
        rows.append({
            "indicator": name, **stats, "beta_to_market": beta, "n_tickers": n_ok,
            "generic_fallback": name in GENERIC_FALLBACK_FUNCTIONS,
        })

    return pd.DataFrame(rows).set_index("indicator").sort_values("sharpe", ascending=False)


if __name__ == "__main__":
    import sys

    invert = "invert" in sys.argv[1:]
    out_path = OUT_CSV.replace(".csv", "_inverted.csv") if invert else OUT_CSV

    start = time.time()
    summary = run_cross_sectional(invert=invert)
    summary.to_csv(out_path)
    elapsed = time.time() - start
    if invert:
        print("\n*** INVERTED: long what each indicator dislikes ***")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 25)
    cols = ["sharpe", "cagr", "net_sharpe", "net_cagr", "avg_turnover",
            "max_drawdown", "beta_to_market", "generic_fallback"]
    print(f"\nRan {len(summary)} indicators as market-neutral cross-sectional books in {elapsed:.1f}s")
    print(f"Results written to {out_path}")
    print(f"Gross exposure 1.0, dollar-neutral, costs charged at {COST_BPS:.0f}bps of turnover\n")
    print(f"{int((summary['sharpe'] > 0).sum())} of {len(summary)} have positive gross Sharpe")
    print(f"{int((summary['net_sharpe'] > 0).sum())} of {len(summary)} still positive after costs")
    print(f"median |beta to market|: {summary['beta_to_market'].abs().median():.4f} "
          f"(near 0 confirms the books are market-neutral)\n")
    print("Top 15 by gross Sharpe:")
    print(summary.head(15)[cols])
