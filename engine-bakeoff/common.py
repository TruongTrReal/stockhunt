"""Shared inputs for the NautilusTrader vs manifoldbt bake-off.

The point of this comparison is to isolate the *engine* — its execution model
and its accounting — from the *signal*. So every engine runs the exact same
five TA-Lib rules over the exact same bars, and each rule is a **stateless**
function of the indicator value on that bar: target position is a pure function
of bar t's indicator, never of the position history. Path dependence would let
two engines diverge for reasons that have nothing to do with their accounting
(a single different entry bar cascades forever), which is the one thing this
harness is trying not to measure.

All rules are long/flat (never short) and fully invested (100% of equity or 0%),
with no fees and no slippage, so the only remaining degrees of freedom are
(a) which price a fill happens at and (b) how the engine compounds equity.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import talib

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

# Which price source every runner reads. Set BAKEOFF_DATA=yfinance to score the
# engines against the project's existing cache instead. Results are written to a
# per-source directory so the two can be compared rather than overwriting.
DATA_SOURCE = os.environ.get("BAKEOFF_DATA", "twelvedata")
YF_CACHE_DIR = REPO_ROOT / "test research" / "data" / "cache"
TD_CACHE_DIR = HERE / "data_td"
RESULTS_DIR = HERE / "results" / DATA_SOURCE

INITIAL_CAPITAL = 10_000.0

# 20 large, liquid names with full history since 2015. Fixed explicitly rather
# than derived from a "top 20 by dollar volume" screen so every engine run —
# and every future rerun — sees an identical universe.
UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]

# TA-Lib warmup: the longest lookback any rule below needs. Bars before this are
# dropped from *every* rule's series so all five rules and all three engines
# start on the same bar with the same capital.
WARMUP_BARS = 60


def load_bars(ticker: str, source: str | None = None) -> pd.DataFrame:
    """Daily OHLCV for one ticker, from whichever source is selected.

    Both sources are dividend- and split-adjusted: the yfinance cache via
    ``auto_adjust=True``, Twelve Data via ``adjust=all``. They still disagree by
    ~1e-4 relative on old bars — see ANALYSIS.md — which is why the two are kept
    separable rather than merged.
    """
    source = source or DATA_SOURCE
    if source == "twelvedata":
        path = TD_CACHE_DIR / f"{ticker}.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing — run twelvedata_loader.py first.")
    elif source == "yfinance":
        path = YF_CACHE_DIR / f"{ticker}.parquet"
    else:
        raise ValueError(f"unknown data source: {source!r}")

    df = pd.read_parquet(path)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(
        {"Open": "float64", "High": "float64", "Low": "float64",
         "Close": "float64", "Volume": "float64"}
    )
    df.index.name = "Date"
    return df.dropna()


# ---------------------------------------------------------------------------
# The five TA-Lib rules
# ---------------------------------------------------------------------------
# Each returns a float array of target positions (1.0 = fully long, 0.0 = flat),
# NaN while the indicator is still warming up. One rule per TA-Lib family that
# the main project's talib_signals.py uses, so the bake-off exercises the same
# shapes of computation the real pipeline does.

def rule_sma_cross(df: pd.DataFrame) -> np.ndarray:
    """Moving-average crossover: long while SMA(20) is above SMA(50)."""
    close = np.asarray(df["Close"], dtype="float64")
    fast, slow = talib.SMA(close, 20), talib.SMA(close, 50)
    return np.where(fast > slow, 1.0, 0.0)


def rule_rsi_trend(df: pd.DataFrame) -> np.ndarray:
    """Zero-line (here: 50-line) oscillator: long while RSI(14) is above 50."""
    rsi = talib.RSI(np.asarray(df["Close"], dtype="float64"), 14)
    return np.where(rsi > 50.0, 1.0, 0.0)


def rule_macd_cross(df: pd.DataFrame) -> np.ndarray:
    """Two-line crossover: long while the MACD line is above its signal line."""
    macd, signal, _ = talib.MACD(np.asarray(df["Close"], dtype="float64"), 12, 26, 9)
    return np.where(macd > signal, 1.0, 0.0)


def rule_cci_zero(df: pd.DataFrame) -> np.ndarray:
    """Zero-line oscillator on a high/low/close indicator: long while CCI(20) > 0."""
    cci = talib.CCI(np.asarray(df["High"], dtype="float64"), np.asarray(df["Low"], dtype="float64"),
                    np.asarray(df["Close"], dtype="float64"), 20)
    return np.where(cci > 0.0, 1.0, 0.0)


def rule_bbands_dip(df: pd.DataFrame) -> np.ndarray:
    """Mean-reversion band: long only while close is below the lower band(20, 2)."""
    close = np.asarray(df["Close"], dtype="float64")
    _, _, lower = talib.BBANDS(close, 20, 2.0, 2.0)
    return np.where(close < lower, 1.0, 0.0)


RULES = {
    "SMA_CROSS": rule_sma_cross,
    "RSI_TREND": rule_rsi_trend,
    "MACD_CROSS": rule_macd_cross,
    "CCI_ZERO": rule_cci_zero,
    "BBANDS_DIP": rule_bbands_dip,
}


def positions_for(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    """All five rules' target positions for one ticker, warmup already trimmed.

    NaN targets (indicator not yet warm) are forced to 0.0 *after* the trim so a
    rule whose own warmup is shorter than WARMUP_BARS can never see a NaN.
    """
    out = pd.DataFrame({name: fn(df) for name, fn in RULES.items()}, index=df.index)
    return out.iloc[WARMUP_BARS:].fillna(0.0)


# ---------------------------------------------------------------------------
# Analytic reference
# ---------------------------------------------------------------------------

def simulate(close: np.ndarray, fill_price: np.ndarray, target: np.ndarray,
             initial_capital: float = INITIAL_CAPITAL) -> dict:
    """Exact share-level simulation of a long/flat, fully-invested rule.

    This is the ground truth both engines are scored against. It is deliberately
    a plain loop over bars rather than the vectorised ``(1 + pos.shift(1) * ret)``
    cumprod the main project uses: the cumprod form is only correct when fills
    happen at the same close the return is measured from, and half the point here
    is to score a next-bar-open engine too.

    Every array is indexed by *the bar the trade happens on*, so a delayed
    execution is expressed by shifting ``target``, not by shifting the price —
    see :func:`execution_arrays`. Getting this backwards (new position applied at
    the close of the bar that generated the signal, but transacted at the next
    bar's open) silently front-runs one bar of return per trade.

    Args:
        close: mark-to-market price per bar.
        fill_price: price a trade executed on bar *t* transacts at.
        target: target equity fraction to hold from bar *t* onward.

    Returns: final equity, trade count, and the full equity curve.
    """
    n = len(close)
    cash, shares = initial_capital, 0.0
    equity = np.empty(n, dtype="float64")
    trades = 0
    prev_target = 0.0

    for i in range(n):
        want = target[i]
        price = fill_price[i]
        if want != prev_target and np.isfinite(price) and price > 0:
            value = cash + shares * price
            new_shares = want * value / price
            cash = value - new_shares * price
            shares = new_shares
            trades += 1
            prev_target = want
        equity[i] = cash + shares * close[i]

    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / initial_capital - 1.0),
        "n_trades": trades,
        "equity": equity,
    }


CONVENTIONS = ("at_close", "next_open")


def execution_arrays(df: pd.DataFrame, target: np.ndarray,
                     convention: str) -> tuple[np.ndarray, np.ndarray]:
    """Re-index a target series onto the bar its trade actually executes on.

    ``at_close``: bar *t*'s signal transacts at bar *t*'s close, so the position
        is already in place when bar *t* is marked. Target and price both stay
        on bar *t*.
    ``next_open``: bar *t*'s signal transacts at bar *t+1*'s open. The position
        therefore only exists from bar *t+1* onward — the target shifts forward
        one bar and the price is that bar's open. Bar 0 has no prior signal, so
        it starts flat.

    Returns ``(exec_target, exec_price)``, both indexed by execution bar.
    """
    if convention == "at_close":
        return target, np.asarray(df["Close"], dtype="float64")
    if convention == "next_open":
        shifted = np.concatenate([[0.0], target[:-1]])
        return shifted, np.asarray(df["Open"], dtype="float64")
    raise ValueError(f"unknown convention: {convention!r}")
