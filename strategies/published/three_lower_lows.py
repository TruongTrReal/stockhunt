"""Long after 3 consecutive lower lows above the 200-day SMA; exit above the prior high."""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib

from strategies._indicators import _bars, D, _state_machine


RULE = 'Long after 3 consecutive lower lows above the 200-day SMA; exit above the prior high.'
SOURCE = "Connors, 'the 3-day high/low' pullback setup"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Three consecutive bars each making a lower low, while price is above its 200-day
    average.

The claim
    A short sequence of lower lows inside an uptrend is orderly profit-taking rather
    than distribution, and tends to be bought.

What the position is really exposed to
    Short-term reversal with a trend gate, like `rsi2_connors` but triggered by
    structure (the sequence) rather than by an oscillator.

How it fails
    'Three' is arbitrary and the rule is sensitive to it. It is also rare, so it trades
    seldom and its statistics rest on few observations.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'n_lows': 3, 'trend_ma_days': 200.0},
        {'n_lows': 2, 'trend_ma_days': 200.0},
        {'n_lows': 4, 'trend_ma_days': 200.0},
        {'n_lows': 3, 'trend_ma_days': 0.0},
)

def position(df, close, bpy, n_lows=3, trend_ma_days=200.0):
    """Connors' pullback count: N consecutive lower lows in an uptrend, exit on strength."""
    lo = df["Low"].to_numpy("float64")
    hi = df["High"].to_numpy("float64")
    lower = pd.Series(lo).diff() < 0
    run = lower.rolling(int(n_lows)).sum().to_numpy() >= n_lows
    entry = np.nan_to_num(run, nan=False)
    if trend_ma_days:
        trend = talib.SMA(close, timeperiod=_bars(bpy, trend_ma_days * D))
        entry &= np.nan_to_num(close > trend, nan=False)
    prev_high = pd.Series(hi).shift(1).to_numpy()
    return _state_machine(entry, np.nan_to_num(close > prev_high, nan=False))
