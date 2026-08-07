"""Minimal vectorized backtest scaffold: SMA(20/50) crossover across the S&P 500 universe.

This is a starting point, not a full backtesting engine — swap `generate_signal`
for your own strategy logic built on the TA-Lib columns from `indicators.py`.
"""

import numpy as np
import pandas as pd

from data_loader import load_universe
from indicators import add_indicators

TRADING_DAYS_PER_YEAR = 252


def generate_signal(df: pd.DataFrame) -> pd.Series:
    """Long when sma_20 > sma_50, flat otherwise. Returns a 0/1 position series."""
    return (df["sma_20"] > df["sma_50"]).astype(int)


def run_backtest(df: pd.DataFrame) -> pd.DataFrame:
    df = add_indicators(df)
    df["position"] = generate_signal(df)
    df["daily_return"] = df["Close"].pct_change()
    # shift(1): trade on the next bar's open-to-close after the signal is known
    df["strategy_return"] = df["position"].shift(1) * df["daily_return"]
    df["equity_curve"] = (1 + df["strategy_return"].fillna(0)).cumprod()
    return df


def performance_stats(df: pd.DataFrame) -> dict:
    returns = df["strategy_return"].dropna()
    if returns.empty or returns.std() == 0:
        return {"cagr": np.nan, "sharpe": np.nan, "max_drawdown": np.nan, "n_days": len(returns)}

    n_years = len(returns) / TRADING_DAYS_PER_YEAR
    cagr = df["equity_curve"].iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else np.nan
    sharpe = (returns.mean() / returns.std()) * np.sqrt(TRADING_DAYS_PER_YEAR)
    running_max = df["equity_curve"].cummax()
    drawdown = df["equity_curve"] / running_max - 1
    max_drawdown = drawdown.min()

    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "n_days": len(returns),
    }


def run_universe_backtest(tickers: list[str] | None = None) -> pd.DataFrame:
    """Run the strategy over every cached ticker and return a per-ticker stats table."""
    data = load_universe(tickers)
    rows = []
    for ticker, df in data.items():
        if len(df) < 60:
            continue
        result = run_backtest(df)
        stats = performance_stats(result)
        stats["ticker"] = ticker
        rows.append(stats)

    stats_df = pd.DataFrame(rows).set_index("ticker").sort_values("sharpe", ascending=False)
    return stats_df


if __name__ == "__main__":
    stats_df = run_universe_backtest()
    print(f"Backtested {len(stats_df)} tickers")
    print(stats_df.describe())
    print("\nTop 10 by Sharpe:")
    print(stats_df.head(10))
