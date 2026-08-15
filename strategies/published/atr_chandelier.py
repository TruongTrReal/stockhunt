"""Long above the 50-day SMA, exited by a 3 x ATR(22) chandelier stop."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D


RULE = 'Long above the 50-day SMA, exited by a 3 x ATR(22) chandelier stop.'
SOURCE = "LeBeau's chandelier exit; Tradeciety / TrendSpider write-ups"
FAMILY = 'volatility'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Long above the 50-day average, exited by a chandelier stop trailing 3 x ATR(22)
    below the highest high since entry.

The claim
    Let winners run and cut losers by volatility rather than by a fixed percentage, so
    the stop means the same thing in calm and turbulent markets.

What the position is really exposed to
    Trend, with a volatility-scaled exit.

How it fails
    The exit is the strategy — entry is a plain moving-average filter. Judge it on
    drawdown, which is what the chandelier stop is for.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 22.0, 'mult': 3.0, 'entry_ma_days': 50.0},
        {'atr_days': 14.0, 'mult': 3.0, 'entry_ma_days': 50.0},
        {'atr_days': 22.0, 'mult': 2.0, 'entry_ma_days': 50.0},
        {'atr_days': 22.0, 'mult': 4.0, 'entry_ma_days': 200.0},
)

def position(df, close, bpy, atr_days=22.0, mult=3.0, entry_ma_days=50.0):
    """Long above a moving average, exited by a LeBeau chandelier stop.

    Path-dependent — the stop hangs from the highest high *since entry* — so this is the
    one strategy here that genuinely needs a bar loop rather than a rolling window.
    """
    n = _bars(bpy, atr_days * D)
    atr = talib.ATR(df["High"].to_numpy("float64"), df["Low"].to_numpy("float64"),
                    close, timeperiod=n)
    ma = talib.SMA(close, timeperiod=_bars(bpy, entry_ma_days * D))
    hi = df["High"].to_numpy("float64")

    out = np.zeros(len(close))
    peak = -np.inf
    for i in range(1, len(close)):
        if out[i - 1] > 0:
            peak = max(peak, hi[i])
            stop = peak - mult * atr[i] if np.isfinite(atr[i]) else -np.inf
            out[i] = 0.0 if close[i] < stop else 1.0
            if out[i] == 0.0:
                peak = -np.inf
        elif np.isfinite(ma[i]) and close[i] > ma[i]:
            out[i], peak = 1.0, hi[i]
    return out
